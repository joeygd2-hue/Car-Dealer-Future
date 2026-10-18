"""Publishing orchestration with idempotent, auditable channel operations.

A listing is never marked published because a button was clicked:
- API channels reach 'published_confirmed' only after the provider confirms.
- Manual channels reach it only when a person records the live URL and attests
  they published it (confirmation_type='manual_attested').
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.config import settings as app_settings
from app.core.audit import audit_ctx
from app.core.crypto import encrypt_secret
from app.core.errors import BadRequest, Conflict, NotFound, Unprocessable
from app.core.jobs import enqueue
from app.core.tenancy import ClientContext
from app.core.time import utcnow
from app.listings.dealer_settings import ListingSettings, get_settings
from app.listings.facts import build_facts
from app.models import ChannelConnection, ChannelPublication, Listing, ListingRevision, PublishOperation, Vehicle
from app.publishing.adapters import ADAPTERS, AdapterError, get_adapter
from app.publishing.adapters.facebook_marketplace import MARKETPLACE_URL_RE

LIVE_STATES = ("submitted", "published_confirmed", "manual_required")


def get_publication(ctx: ClientContext, pub_id: str) -> ChannelPublication:
    pub = ctx.db.get(ChannelPublication, pub_id)
    if not pub or pub.client_id != ctx.client_id:
        raise NotFound("Publication not found")
    return pub


def get_connection(ctx: ClientContext, channel: str) -> ChannelConnection | None:
    return ctx.db.scalar(select(ChannelConnection).where(ChannelConnection.channel == channel))


def upsert_connection(ctx: ClientContext, channel: str, *, config: dict, secret: str | None = None,
                      live_publishing: bool | None = None) -> ChannelConnection:
    if channel not in ADAPTERS:
        raise BadRequest(f"Unknown channel '{channel}'")
    conn = get_connection(ctx, channel)
    if not conn:
        conn = ChannelConnection(client_id=ctx.client_id, channel=channel)
        ctx.db.add(conn)
    conn.config = {k: v for k, v in (config or {}).items() if "token" not in k.lower() and "secret" not in k.lower()}
    if secret:
        conn.secret_ciphertext = encrypt_secret(secret)
    if live_publishing is not None:
        conn.live_publishing = bool(live_publishing)
        if live_publishing:
            conn.authorized_by, conn.authorized_at = ctx.actor.id, utcnow()
    ctx.db.flush()
    audit_ctx(ctx, "channel_connection.updated", "channel_connection", conn.id, channel=channel,
              live_publishing=conn.live_publishing, secret_updated=bool(secret))
    return conn


def _payload(ctx: ClientContext, pub: ChannelPublication) -> tuple[dict, list[str], ListingRevision, Vehicle]:
    lst = ctx.db.get(Listing, pub.listing_id)
    rev = ctx.db.get(ListingRevision, lst.approved_revision_id) if lst.approved_revision_id else None
    if not rev:
        raise Unprocessable("Listing has no approved revision")
    v = ctx.db.get(Vehicle, pub.vehicle_id)
    s, _ = get_settings(ctx)
    adapter = get_adapter(pub.channel)
    conn = get_connection(ctx, pub.channel)
    payload = adapter.build_payload(content=rev.content, vehicle=v, facts=build_facts(ctx.db, v, s),
                                    connection_config=(conn.config if conn else {}) or {})
    return payload, adapter.validate_payload(payload), rev, v


def on_listing_approved(ctx: ClientContext, lst: Listing, s: ListingSettings) -> list[ChannelPublication]:
    pubs = []
    for channel in s.enabled_channels:
        if channel not in ADAPTERS:
            continue
        pub = ctx.db.scalar(select(ChannelPublication).where(ChannelPublication.listing_id == lst.id,
                                                             ChannelPublication.channel == channel))
        if not pub:
            pub = ChannelPublication(client_id=ctx.client_id, listing_id=lst.id, vehicle_id=lst.vehicle_id, channel=channel,
                                     status="ready_to_publish")
            ctx.db.add(pub)
            ctx.db.flush()
        elif pub.status in ("published_confirmed", "submitted", "manual_required"):
            pub.needs_update, pub.update_reason = True, "A new revision was approved; update the live listing"
        elif pub.status in ("withdrawn", "failed"):
            pub.status = "ready_to_publish"
        pubs.append(pub)
        if get_adapter(channel).mode == "manual" and pub.status == "ready_to_publish":
            prepare_manual(ctx, pub)
    return pubs


def _op(ctx: ClientContext, pub: ChannelPublication, operation: str, key: str, request: dict) -> tuple[PublishOperation, bool]:
    existing = ctx.db.scalar(select(PublishOperation).where(PublishOperation.idempotency_key == key))
    if existing:
        return existing, False
    op = PublishOperation(client_id=ctx.client_id, publication_id=pub.id, operation=operation, idempotency_key=key,
                          status="pending", request=request, actor_id=ctx.actor.id or ctx.actor.type)
    ctx.db.add(op)
    ctx.db.flush()
    return op, True


def prepare_manual(ctx: ClientContext, pub: ChannelPublication) -> PublishOperation:
    payload, problems, rev, v = _payload(ctx, pub)
    op, _ = _op(ctx, pub, "prepare_manual", f"{pub.id}:prepare:{rev.id}:v{v.version}", payload)
    op.status, op.response = "succeeded", {"problems": problems}
    if pub.status == "ready_to_publish":
        pub.status = "manual_required"
    pub.last_error = "; ".join(problems) or None
    audit_ctx(ctx, "publication.manual_package_prepared", "publication", pub.id, channel=pub.channel, problems=problems)
    ctx.db.flush()
    return op


def request_publish(ctx: ClientContext, pub_id: str) -> PublishOperation:
    """User asks to publish/update an approved listing on an API channel. Idempotent."""
    pub = get_publication(ctx, pub_id)
    adapter = get_adapter(pub.channel)
    lst = ctx.db.get(Listing, pub.listing_id)
    v = ctx.db.get(Vehicle, pub.vehicle_id)
    if lst.status != "approved":
        raise Unprocessable("Only approved listings can be published")
    if v.inventory_status not in ("available", "pending") or v.stale:
        raise Unprocessable("Vehicle availability is not confirmed; it cannot be published")
    if adapter.mode == "manual":
        return prepare_manual(ctx, pub)
    payload, problems, rev, v = _payload(ctx, pub)
    if problems:
        pub.last_error = "; ".join(problems)
        ctx.db.flush()
        raise Unprocessable("Listing is missing data this channel requires", {"problems": problems})
    is_update = pub.status in ("published_confirmed", "submitted") and pub.external_id
    operation = "update" if is_update else "create"
    op, created = _op(ctx, pub, operation, f"{pub.id}:{operation}:{rev.id}:v{v.version}", payload)
    if created:
        enqueue(ctx.db, "publishing.execute", client_id=ctx.client_id, payload={"operation_id": op.id},
                max_attempts=app_settings.publish_max_attempts, idempotency_key=f"exec:{op.id}")
        audit_ctx(ctx, "publication.requested", "publication", pub.id, channel=pub.channel, operation=operation, op_id=op.id)
    return op


def execute_operation(ctx: ClientContext, op_id: str, *, final_attempt: bool = False) -> dict[str, Any]:
    """Run by the worker. Raises AdapterError(retryable=True) to trigger a job retry."""
    op = ctx.db.get(PublishOperation, op_id)
    if not op or op.client_id != ctx.client_id:
        raise NotFound("Operation not found")
    if op.status in ("succeeded", "dead"):
        return {"skipped": f"operation already {op.status}"}
    pub = ctx.db.get(ChannelPublication, op.publication_id)
    adapter = get_adapter(pub.channel)
    conn = get_connection(ctx, pub.channel)
    v = ctx.db.get(Vehicle, pub.vehicle_id)
    if op.operation in ("create", "update") and (v.inventory_status not in ("available", "pending") or v.stale):
        op.status, op.error = "dead", "Vehicle no longer available; publish cancelled"
        ctx.db.flush()
        return {"cancelled": op.error}
    op.attempt_count += 1
    try:
        if op.operation == "delete":
            res = adapter.delete(connection=conn, external_id=pub.external_id, idempotency_key=op.idempotency_key, payload=op.request)
        elif op.operation == "update":
            res = adapter.update(op.request, connection=conn, idempotency_key=op.idempotency_key, external_id=pub.external_id)
        elif op.operation == "status_check":
            res = adapter.check_status(connection=conn, provider_ref=op.provider_ref or op.request.get("provider_ref"), payload=op.request)
        else:
            res = adapter.publish(op.request, connection=conn, idempotency_key=op.idempotency_key)
    except AdapterError as exc:
        op.error = str(exc)
        pub.last_error = str(exc)
        if exc.retryable and not final_attempt:
            ctx.db.commit()  # keep attempt/error history; the job retries with backoff
            raise
        op.status = "dead" if exc.retryable else "failed"
        pub.failure_count += 1
        pub.status = "failed" if op.operation in ("create", "update") else pub.status
        audit_ctx(ctx, "publication.failed", "publication", pub.id, channel=pub.channel, operation=op.operation, error=str(exc))
        ctx.db.flush()
        return {"failed": str(exc)}

    op.response, op.provider_ref = res.response, res.provider_ref or op.provider_ref
    if res.outcome == "failed":
        op.status, op.error = "failed", res.error
        pub.failure_count += 1
        pub.last_error = res.error
        if op.operation in ("create", "update", "status_check"):
            pub.status = "failed"
        audit_ctx(ctx, "publication.failed", "publication", pub.id, channel=pub.channel, operation=op.operation, error=res.error)
    elif op.operation == "delete":
        op.status = "succeeded"
        if res.outcome == "submitted":
            _schedule_check(ctx, pub, res.provider_ref, final_status="withdrawn")
        else:
            pub.status = "withdrawn"
            audit_ctx(ctx, "publication.withdrawn", "publication", pub.id, channel=pub.channel, confirmation="api")
    elif res.outcome == "submitted" and op.operation == "status_check":
        op.error = "Provider still processing"
        ctx.db.commit()
        raise AdapterError("Provider is still processing", retryable=True)
    elif res.outcome == "submitted":
        op.status = "succeeded"
        pub.status = "submitted"
        pub.external_id = res.external_id or pub.external_id
        pub.external_url = res.external_url or pub.external_url
        if op.operation in ("create", "update"):
            lst = ctx.db.get(Listing, pub.listing_id)
            pub.published_revision_id, pub.published_vehicle_version = lst.approved_revision_id, v.version
        _schedule_check(ctx, pub, res.provider_ref)
        audit_ctx(ctx, "publication.submitted", "publication", pub.id, channel=pub.channel, provider_ref=res.provider_ref)
    elif res.outcome == "confirmed":
        op.status = "succeeded"
        if op.operation == "status_check" and op.request.get("final_status") == "withdrawn":
            pub.status = "withdrawn"
            audit_ctx(ctx, "publication.withdrawn", "publication", pub.id, channel=pub.channel, confirmation="api")
        else:
            pub.status, pub.confirmation_type, pub.confirmed_at = "published_confirmed", "api", utcnow()
            pub.needs_update, pub.update_reason, pub.last_error = False, None, None
            audit_ctx(ctx, "publication.confirmed", "publication", pub.id, channel=pub.channel, confirmation="api")
    elif res.outcome == "manual_required":
        op.status = "succeeded"
        pub.status = "manual_required" if pub.status != "published_confirmed" else pub.status
    ctx.db.flush()
    return {"outcome": res.outcome, "publication_status": pub.status}


def _schedule_check(ctx: ClientContext, pub: ChannelPublication, provider_ref: str | None, final_status: str | None = None) -> None:
    if not provider_ref:
        return
    op, created = _op(ctx, pub, "status_check", f"{pub.id}:check:{provider_ref}",
                      {"provider_ref": provider_ref, "final_status": final_status})
    op.provider_ref = provider_ref
    if created:
        enqueue(ctx.db, "publishing.execute", client_id=ctx.client_id, payload={"operation_id": op.id},
                max_attempts=app_settings.publish_max_attempts, idempotency_key=f"exec:{op.id}")


def retry_publication(ctx: ClientContext, pub_id: str) -> PublishOperation:
    pub = get_publication(ctx, pub_id)
    if pub.status != "failed":
        raise Conflict("Only failed publications can be retried")
    pub.status = "ready_to_publish" if not pub.external_id else "published_confirmed"
    payload, problems, rev, v = _payload(ctx, pub)
    operation = "update" if pub.external_id else "create"
    op, created = _op(ctx, pub, operation, f"{pub.id}:{operation}:{rev.id}:v{v.version}:retry{pub.failure_count}", payload)
    if created:
        enqueue(ctx.db, "publishing.execute", client_id=ctx.client_id, payload={"operation_id": op.id},
                max_attempts=app_settings.publish_max_attempts, idempotency_key=f"exec:{op.id}")
    audit_ctx(ctx, "publication.retry_requested", "publication", pub.id, op_id=op.id)
    return op


def withdraw(ctx: ClientContext, pub: ChannelPublication, reason: str) -> None:
    """Vehicle sold/removed: stop advertising it."""
    adapter = get_adapter(pub.channel)
    if pub.status in ("withdrawn",):
        return
    never_live = pub.status in ("ready_to_publish", "failed") and not pub.external_id
    if never_live:
        pub.status = "withdrawn"
    elif adapter.mode == "manual":
        pub.status, pub.update_reason = "withdrawal_required", reason
    else:
        pub.status, pub.update_reason = "withdrawal_required", reason
        op, created = _op(ctx, pub, "delete", f"{pub.id}:delete:{pub.external_id}:{reason}", {"vehicle_id": pub.external_id or pub.vehicle_id})
        if created:
            enqueue(ctx.db, "publishing.execute", client_id=ctx.client_id, payload={"operation_id": op.id},
                    max_attempts=app_settings.publish_max_attempts, idempotency_key=f"exec:{op.id}")
    audit_ctx(ctx, "publication.withdrawal_flagged", "publication", pub.id, channel=pub.channel, reason=reason, status=pub.status)
    ctx.db.flush()


def record_manual_publication(ctx: ClientContext, pub_id: str, *, url: str, attested: bool, notes: str | None = None) -> ChannelPublication:
    pub = get_publication(ctx, pub_id)
    if get_adapter(pub.channel).mode != "manual":
        raise BadRequest("Manual confirmation is only for manual channels")
    if not attested:
        raise BadRequest("Confirm that you personally published this listing and checked it matches the package")
    if not MARKETPLACE_URL_RE.match(url or ""):
        raise BadRequest("Enter the live listing URL (https://www.facebook.com/marketplace/item/...)")
    lst = ctx.db.get(Listing, pub.listing_id)
    v = ctx.db.get(Vehicle, pub.vehicle_id)
    if pub.status not in ("manual_required", "published_confirmed"):
        raise Conflict(f"Publication is {pub.status}")
    if lst.status != "approved" or v.inventory_status in ("sold", "removed"):
        raise Conflict("Listing is no longer approved/available; do not publish it")
    op, _ = _op(ctx, pub, "confirm_manual", f"{pub.id}:confirm:{lst.approved_revision_id}:{url}", {"url": url, "notes": notes})
    op.status = "succeeded"
    pub.status, pub.external_url, pub.external_id = "published_confirmed", url, url.rstrip("/").split("/")[-1].split("?")[0]
    pub.confirmation_type, pub.confirmed_by, pub.confirmed_at = "manual_attested", ctx.actor.id, utcnow()
    pub.published_revision_id, pub.published_vehicle_version = lst.approved_revision_id, v.version
    pub.needs_update, pub.update_reason, pub.last_error = False, None, None
    audit_ctx(ctx, "publication.manual_confirmed", "publication", pub.id, url=url, notes=notes)
    ctx.db.flush()
    return pub


def record_manual_update(ctx: ClientContext, pub_id: str, *, attested: bool, notes: str | None = None) -> ChannelPublication:
    pub = get_publication(ctx, pub_id)
    if not attested:
        raise BadRequest("Confirm you updated the live listing to match the current approved content")
    if pub.status != "published_confirmed":
        raise Conflict("Only published listings can be marked updated")
    lst = ctx.db.get(Listing, pub.listing_id)
    if lst.status != "approved":
        raise Conflict("Approve the current revision before recording the update")
    v = ctx.db.get(Vehicle, pub.vehicle_id)
    op, _ = _op(ctx, pub, "record_manual_update", f"{pub.id}:mupdate:{lst.approved_revision_id}:v{v.version}", {"notes": notes})
    op.status = "succeeded"
    pub.needs_update, pub.update_reason = False, None
    pub.published_revision_id, pub.published_vehicle_version = lst.approved_revision_id, v.version
    audit_ctx(ctx, "publication.manual_updated", "publication", pub.id, notes=notes)
    ctx.db.flush()
    return pub


def record_manual_removal(ctx: ClientContext, pub_id: str, *, attested: bool, notes: str | None = None) -> ChannelPublication:
    pub = get_publication(ctx, pub_id)
    if not attested:
        raise BadRequest("Confirm the listing was deleted (or was never posted) on the channel")
    op, _ = _op(ctx, pub, "record_manual_removal", f"{pub.id}:mremove:{pub.external_id or 'none'}", {"notes": notes})
    op.status = "succeeded"
    pub.status, pub.needs_update = "withdrawn", False
    audit_ctx(ctx, "publication.manual_removed", "publication", pub.id, notes=notes)
    ctx.db.flush()
    return pub


def publication_view(ctx: ClientContext, pub: ChannelPublication, *, with_package: bool = False) -> dict[str, Any]:
    adapter = get_adapter(pub.channel)
    out = {"id": pub.id, "listing_id": pub.listing_id, "vehicle_id": pub.vehicle_id, "channel": pub.channel,
           "channel_label": adapter.label, "mode": adapter.mode, "status": pub.status, "needs_update": pub.needs_update,
           "update_reason": pub.update_reason, "external_url": pub.external_url, "confirmation_type": pub.confirmation_type,
           "confirmed_at": pub.confirmed_at.isoformat() if pub.confirmed_at else None, "last_error": pub.last_error,
           "failure_count": pub.failure_count}
    if with_package:
        try:
            payload, problems, _, _ = _payload(ctx, pub)
            out["package"], out["package_problems"] = payload, problems
        except Unprocessable as exc:
            out["package_problems"] = [exc.message]
        ops = ctx.db.scalars(select(PublishOperation).where(PublishOperation.publication_id == pub.id)
                             .order_by(PublishOperation.created_at)).all()
        out["operations"] = [{"id": o.id, "operation": o.operation, "status": o.status, "attempts": o.attempt_count,
                              "error": o.error, "created_at": o.created_at.isoformat()} for o in ops]
    return out

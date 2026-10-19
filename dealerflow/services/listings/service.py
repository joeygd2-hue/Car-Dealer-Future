"""Listing lifecycle: generate -> review -> approve/reject -> publish readiness.

Statuses (Listing.status):
  imported -> needs_validation (vehicle has blocking data errors)
           -> draft_generated (content exists but has blocking guardrail errors)
           -> awaiting_approval -> approved | rejected
  any -> sold_withdrawn (vehicle sold/removed)
Channel-level states (ready_to_publish, submitted, published_confirmed,
manual_required, failed, withdrawal_required, withdrawn) live on
ChannelPublication; see app/publishing/service.py.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

import secrets

from ...audit import audit
from ...config import get_settings as app_settings
from ...db import utcnow
from ...models_dealer import Listing, ListingReview, ListingRevision, Vehicle
from ...tenancy import ClientContext
from ..dealer_errors import BadRequest, Conflict, Forbidden, NotFound, Unprocessable
from . import ai_generator, template_generator
from .dealer_settings import ListingSettings, get_settings
from .facts import build_facts, facts_hash
from .guardrails import check_content, warning_key


def _audit(ctx: ClientContext, action: str, entity_type: str, entity_id: int | None, **data) -> None:
    audit(ctx.db, action, actor_id=ctx.actor_id, entity_type=entity_type, entity_id=entity_id,
          data={"client_id": ctx.client_id, **data})

INACTIVE = ("sold", "removed")


def _vehicle(ctx: ClientContext, vehicle_id: int) -> Vehicle:
    v = ctx.db.get(Vehicle, vehicle_id)
    if not v or v.client_id != ctx.client_id:
        raise NotFound("Vehicle not found")
    return v


def get_listing(ctx: ClientContext, listing_id: int) -> Listing:
    lst = ctx.db.get(Listing, listing_id)
    if not lst or lst.client_id != ctx.client_id:
        raise NotFound("Listing not found")
    return lst


def ensure_listing(ctx: ClientContext, vehicle: Vehicle) -> Listing:
    lst = ctx.db.scalar(select(Listing).where(Listing.vehicle_id == vehicle.id))
    if not lst:
        lst = Listing(client_id=ctx.client_id, vehicle_id=vehicle.id,
                      status="needs_validation" if vehicle.validation_status == "invalid" else "imported")
        ctx.db.add(lst)
        ctx.db.flush()
    return lst


def current_revision(ctx: ClientContext, lst: Listing) -> ListingRevision | None:
    return ctx.db.get(ListingRevision, lst.current_revision_id) if lst.current_revision_id else None


def _next_number(ctx: ClientContext, lst: Listing) -> int:
    return (ctx.db.scalar(select(func.max(ListingRevision.revision_number)).where(ListingRevision.listing_id == lst.id)) or 0) + 1


def _status_after_revision(guard: dict, s: ListingSettings) -> str:
    if guard["errors"]:
        return "draft_generated"
    if not s.review.require_approval:
        return "awaiting_approval"  # still requires an explicit approve call; never auto-publish
    return "awaiting_approval" if s.review.auto_submit_for_approval else "draft_generated"


def _store_revision(ctx: ClientContext, lst: Listing, v: Vehicle, facts: dict, content: dict, uncertainty: list[str],
                    origin: str, s_version: int, s: ListingSettings, model: str | None = None,
                    feedback: str | None = None) -> ListingRevision:
    guard = check_content(facts, content, s)
    rev = ListingRevision(client_id=ctx.client_id, listing_id=lst.id, revision_number=_next_number(ctx, lst), origin=origin,
                          generator_model=model, content=content, facts=facts, facts_hash=facts_hash(facts),
                          vehicle_version=v.version, settings_version=s_version, guardrails=guard, uncertainty=uncertainty,
                          feedback=feedback, created_by_id=ctx.actor_id, created_by_label=ctx.actor_label)
    ctx.db.add(rev)
    ctx.db.flush()
    lst.current_revision_id = rev.id
    if lst.status != "sold_withdrawn":
        lst.status = _status_after_revision(guard, s)
    lst.content_outdated = False
    lst.outdated_reason = None
    return rev


def generate_listing(ctx: ClientContext, listing_id: int, *, feedback: str | None = None, use_ai: bool | None = None) -> ListingRevision:
    lst = get_listing(ctx, listing_id)
    v = _vehicle(ctx, lst.vehicle_id)
    if v.validation_status == "invalid":
        lst.status = "needs_validation"
        ctx.db.flush()
        errors = [i["message"] for i in v.validation_issues if i["severity"] == "error"]
        raise Unprocessable("Fix the vehicle data before generating a listing", {"validation_errors": errors})
    if v.inventory_status in INACTIVE:
        raise Conflict(f"Vehicle is {v.inventory_status}; listings are not generated for it")
    s, s_version = get_settings(ctx)
    facts = build_facts(ctx.db, v, s)
    ai_ok = app_settings().listing_ai_available
    want_ai = ai_ok if use_ai is None else (use_ai and ai_ok)
    model, meta = None, None
    if want_ai:
        try:
            content, uncertainty, meta = ai_generator.generate(facts, s, feedback)
            origin, model = "ai_generator", meta["model"]
        except Exception as exc:  # noqa: BLE001 - fall back, but say so
            content, uncertainty = template_generator.generate(facts, s)
            uncertainty.append(f"AI generation unavailable ({type(exc).__name__}); used the template generator")
            origin = "template_generator"
    else:
        content, uncertainty = template_generator.generate(facts, s)
        origin = "template_generator"
        if feedback:
            uncertainty.append("Reviewer feedback recorded; the template generator cannot rewrite prose. Edit the text directly "
                               "or enable AI generation.")
    rev = _store_revision(ctx, lst, v, facts, content, uncertainty, origin, s_version, s, model=model, feedback=feedback)
    _audit(ctx, "listing.generated", "listing", lst.id, revision=rev.revision_number, origin=origin,
           errors=len(rev.guardrails["errors"]), warnings=len(rev.guardrails["warnings"]), **({"ai": meta} if meta else {}))
    # Measured AI cost (from API token usage) is kept in the audit entry above. It is deliberately not written to
    # cost_entries, which Phase 1 uses for customer-acquisition cost.
    ctx.db.flush()
    return rev


EDITABLE = ("title", "description", "call_to_action", "search_terms", "faq", "features")


def edit_listing(ctx: ClientContext, listing_id: int, changes: dict[str, dict[str, Any]], note: str | None = None) -> ListingRevision:
    lst = get_listing(ctx, listing_id)
    base = current_revision(ctx, lst)
    if not base:
        raise BadRequest("Generate a draft before editing")
    v = _vehicle(ctx, lst.vehicle_id)
    s, s_version = get_settings(ctx)
    facts = build_facts(ctx.db, v, s)  # edits are always checked against current facts
    content = {k: (dict(val) if isinstance(val, dict) else val) for k, val in base.content.items()}
    for lang, fields in (changes or {}).items():
        if lang not in ("en", "es"):
            raise BadRequest(f"Unknown language '{lang}'")
        bad = set(fields) - set(EDITABLE)
        if bad:
            raise BadRequest(f"Fields not editable: {', '.join(sorted(bad))}")
        content.setdefault(lang, {}).update(fields)
    rev = _store_revision(ctx, lst, v, facts, content, list(base.uncertainty), "human_edit", s_version, s, feedback=note)
    _audit(ctx, "listing.edited", "listing", lst.id, revision=rev.revision_number,
           fields={k: sorted(f) for k, f in (changes or {}).items()}, errors=len(rev.guardrails["errors"]))
    ctx.db.flush()
    return rev


def _can_approve(ctx: ClientContext, s: ListingSettings) -> None:
    if not ctx.can("approve"):
        raise Forbidden("Your role cannot approve listings")
    if ctx.is_staff and ctx.user is not None and not s.review.allow_agency_approval:
        raise Forbidden("This dealership requires its own staff to approve listings")


def approval_blockers(ctx: ClientContext, lst: Listing, rev: ListingRevision | None, s: ListingSettings,
                      acknowledged: set[str] | None = None) -> list[str]:
    blockers: list[str] = []
    if not rev:
        return ["No draft has been generated"]
    v = _vehicle(ctx, lst.vehicle_id)
    if v.inventory_status in INACTIVE:
        blockers.append(f"Vehicle is {v.inventory_status}")
    if v.stale:
        blockers.append("Vehicle availability is unconfirmed (stale inventory data)")
    if v.validation_status == "invalid":
        blockers.append("Vehicle data has blocking validation errors")
    if lst.status not in ("awaiting_approval", "draft_generated", "rejected"):
        blockers.append(f"Listing is {lst.status}")
    if rev.id != lst.current_revision_id:
        blockers.append("A newer revision exists")
    if facts_hash(build_facts(ctx.db, v, s)) != rev.facts_hash:
        blockers.append("Vehicle data changed since this draft was generated; regenerate first")
    if rev.guardrails["errors"]:
        blockers.append(f"{len(rev.guardrails['errors'])} blocking content issue(s)")
    if acknowledged is not None:
        missing = [w for w in rev.guardrails["warnings"] if warning_key(w) not in acknowledged]
        if missing:
            blockers.append(f"{len(missing)} warning(s) not acknowledged")
    return blockers


def approve_listing(ctx: ClientContext, listing_id: int, revision_id: int, acknowledged_warnings: list[str] | None = None,
                    note: str | None = None, batch_id: str | None = None) -> Listing:
    s, _ = get_settings(ctx)
    _can_approve(ctx, s)
    lst = get_listing(ctx, listing_id)
    rev = current_revision(ctx, lst)
    if not rev or rev.id != revision_id:
        raise Conflict("You are approving an outdated revision; reload and review the current one")
    ack = set(acknowledged_warnings or [])
    blockers = approval_blockers(ctx, lst, rev, s, ack)
    if blockers:
        raise Unprocessable("Listing cannot be approved", {"blockers": blockers})
    lst.status, lst.approved_revision_id, lst.approved_by_id, lst.approved_at = "approved", rev.id, ctx.actor_id, utcnow()
    ctx.db.add(ListingReview(client_id=ctx.client_id, listing_id=lst.id, revision_id=rev.id, decision="approved", note=note,
                             acknowledged_warnings=sorted(ack), batch_id=batch_id, user_id=ctx.actor_id))
    _audit(ctx, "listing.approved", "listing", lst.id, revision=rev.revision_number, batch_id=batch_id,
           acknowledged_warnings=sorted(ack))
    ctx.db.flush()
    from ..publishing.service import on_listing_approved
    on_listing_approved(ctx, lst, s)
    return lst


def reject_listing(ctx: ClientContext, listing_id: int, note: str) -> Listing:
    if not (note or "").strip():
        raise BadRequest("A reason is required when rejecting")
    s, _ = get_settings(ctx)
    _can_approve(ctx, s)
    lst = get_listing(ctx, listing_id)
    rev = current_revision(ctx, lst)
    if not rev:
        raise BadRequest("Nothing to reject")
    lst.status = "rejected"
    ctx.db.add(ListingReview(client_id=ctx.client_id, listing_id=lst.id, revision_id=rev.id, decision="rejected", note=note,
                             user_id=ctx.actor_id))
    _audit(ctx, "listing.rejected", "listing", lst.id, revision=rev.revision_number, note=note)
    ctx.db.flush()
    return lst


def request_regeneration(ctx: ClientContext, listing_id: int, note: str | None) -> ListingRevision:
    lst = get_listing(ctx, listing_id)
    rev = current_revision(ctx, lst)
    if rev:
        ctx.db.add(ListingReview(client_id=ctx.client_id, listing_id=lst.id, revision_id=rev.id,
                                 decision="regeneration_requested", note=note, user_id=ctx.actor_id))
    return generate_listing(ctx, listing_id, feedback=note)


def batch_preview(ctx: ClientContext, listing_ids: list[int]) -> dict[str, Any]:
    s, _ = get_settings(ctx)
    if len(listing_ids) > s.review.max_batch_size:
        raise BadRequest(f"Batch approvals are limited to {s.review.max_batch_size} listings")
    items = []
    for lid in dict.fromkeys(listing_ids):
        try:
            lst = get_listing(ctx, lid)
        except NotFound:
            items.append({"listing_id": lid, "eligible": False, "blockers": ["Not found"], "warnings": []})
            continue
        rev = current_revision(ctx, lst)
        blockers = approval_blockers(ctx, lst, rev, s)
        items.append({"listing_id": lid, "revision_id": rev.id if rev else None, "eligible": not blockers, "blockers": blockers,
                      "warnings": [warning_key(w) for w in (rev.guardrails["warnings"] if rev else [])],
                      "title": (rev.content.get("en") or {}).get("title") if rev else None})
    return {"items": items, "eligible_count": sum(i["eligible"] for i in items),
            "with_warnings": sum(1 for i in items if i["eligible"] and i["warnings"])}


def batch_approve(ctx: ClientContext, listing_ids: list[int], *, expected_count: int, confirm: bool,
                  acknowledge_all_warnings: bool = False, note: str | None = None) -> dict[str, Any]:
    """Safeguards: explicit confirm, expected eligible count must match the preview,
    size limit, per-item revalidation, warnings require an explicit acknowledgement."""
    if not confirm:
        raise BadRequest("Batch approval requires confirm=true")
    s, _ = get_settings(ctx)
    _can_approve(ctx, s)
    preview = batch_preview(ctx, listing_ids)
    if preview["eligible_count"] != expected_count:
        raise Conflict("The set of eligible listings changed since you reviewed it; preview again",
                       {"eligible_now": preview["eligible_count"], "expected": expected_count})
    batch_id = "bat_" + secrets.token_hex(8)
    approved, skipped = [], []
    for item in preview["items"]:
        if not item["eligible"]:
            skipped.append({"listing_id": item["listing_id"], "reasons": item["blockers"]})
            continue
        if item["warnings"] and not acknowledge_all_warnings:
            skipped.append({"listing_id": item["listing_id"], "reasons": ["Has warnings that were not acknowledged"]})
            continue
        approve_listing(ctx, item["listing_id"], item["revision_id"], item["warnings"], note=note, batch_id=batch_id)
        approved.append(item["listing_id"])
    _audit(ctx, "listing.batch_approved", "listing_batch", None, batch_id=batch_id, approved=len(approved), skipped=len(skipped))
    return {"batch_id": batch_id, "approved": approved, "skipped": skipped}


def mark_outdated(ctx: ClientContext, vehicle: Vehicle, reason: str, *, regenerate: bool) -> Listing | None:
    lst = ctx.db.scalar(select(Listing).where(Listing.vehicle_id == vehicle.id))
    if not lst or lst.status == "sold_withdrawn":
        return lst
    lst.content_outdated, lst.outdated_reason = True, reason
    if lst.status == "approved":
        lst.status = "awaiting_approval" if lst.current_revision_id else "imported"
    if vehicle.validation_status == "invalid":
        lst.status = "needs_validation"
    ctx.db.flush()
    if regenerate and vehicle.validation_status != "invalid" and lst.current_revision_id:
        from ...jobs import enqueue
        enqueue(ctx.db, "listing_generate", f"listing_generate:{lst.id}:v{vehicle.version}",
                {"client_id": ctx.client_id, "listing_id": lst.id, "reason": reason})
    return lst


def listing_view(ctx: ClientContext, lst: Listing, *, full: bool = True) -> dict[str, Any]:
    v = _vehicle(ctx, lst.vehicle_id)
    rev = current_revision(ctx, lst)
    s, _ = get_settings(ctx)
    out: dict[str, Any] = {
        "id": lst.id, "vehicle_id": v.id, "status": lst.status, "content_outdated": lst.content_outdated,
        "outdated_reason": lst.outdated_reason, "approved_revision_id": lst.approved_revision_id,
        "approved_at": lst.approved_at.isoformat() if lst.approved_at else None, "approved_by_id": lst.approved_by_id,
        "current_revision": None,
    }
    if rev:
        out["current_revision"] = {
            "id": rev.id, "number": rev.revision_number, "origin": rev.origin, "model": rev.generator_model,
            "content": rev.content if full else {k: {"title": rev.content[k]["title"]} for k in ("en", "es") if k in rev.content},
            "guardrails": rev.guardrails, "warning_keys": [warning_key(w) for w in rev.guardrails["warnings"]],
            "uncertainty": rev.uncertainty, "created_at": rev.created_at.isoformat(), "created_by": rev.created_by_label,
        }
        out["approval_blockers"] = approval_blockers(ctx, lst, rev, s)
    return out

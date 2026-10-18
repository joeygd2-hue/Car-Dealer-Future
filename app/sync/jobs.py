"""Background job handlers for inventory sync, listing generation, publishing,
stale detection and failure sweeps. Registered with the shared worker."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.audit import audit, audit_ctx
from app.core.jobs import enqueue, upsert_schedule, remove_schedule
from app.core.tenancy import job_context
from app.core.time import utcnow
from app.core.worker import NonRetryableError
from app.inventory.feeds import FEED_TYPES, sync_feed_source
from app.listings.service import generate_listing
from app.models import ChannelPublication, Client, InventorySource, Job, Listing, Vehicle, VehicleChange
from app.publishing.adapters import AdapterError
from app.publishing.service import execute_operation


def _ctx(job: Job, name: str):
    if not job.client_id:
        raise NonRetryableError("Job has no client_id")
    return job_context(job.client_id, name)


def handle_sync_source(db, job: Job, payload: dict) -> dict:
    ctx = _ctx(job, "inventory.sync_source")
    try:
        src = ctx.db.get(InventorySource, payload["source_id"])
        if not src or not src.enabled or src.source_type not in FEED_TYPES:
            return {"skipped": "source disabled or not a feed"}
        try:
            run = sync_feed_source(ctx, src, trigger="schedule")
        except Exception as exc:
            src.last_status, src.last_sync_at = "failed", utcnow()
            audit_ctx(ctx, "inventory.sync_failed", "inventory_source", src.id, error=str(exc)[:500])
            ctx.db.commit()
            raise
        ctx.db.commit()
        return {"run_id": run.id, "status": run.status, "counts": {k: v for k, v in run.counts.items() if isinstance(v, int)}}
    finally:
        ctx.db.close()


def handle_generate(db, job: Job, payload: dict) -> dict:
    ctx = _ctx(job, "listings.generate")
    try:
        lst = ctx.db.get(Listing, payload["listing_id"])
        if not lst or lst.status == "sold_withdrawn":
            return {"skipped": "listing inactive"}
        v = ctx.db.get(Vehicle, lst.vehicle_id)
        if v.validation_status == "invalid":
            lst.status = "needs_validation"
            ctx.db.commit()
            return {"skipped": "vehicle invalid"}
        rev = generate_listing(ctx, lst.id)
        ctx.db.commit()
        return {"revision_id": rev.id, "errors": len(rev.guardrails["errors"])}
    finally:
        ctx.db.close()


def handle_publish_execute(db, job: Job, payload: dict) -> dict:
    ctx = _ctx(job, "publishing.execute")
    try:
        final = job.attempts >= job.max_attempts
        try:
            result = execute_operation(ctx, payload["operation_id"], final_attempt=final)
        except AdapterError as exc:
            raise RuntimeError(str(exc)) from exc  # retryable -> worker backoff
        ctx.db.commit()
        return result
    finally:
        ctx.db.close()


def handle_stale_scan(db, job: Job, payload: dict) -> dict:
    """Flag vehicles whose availability can no longer be confirmed, and overdue confirmations."""
    now = utcnow()
    flagged = overdue = 0
    for client in db.scalars(select(Client).where(Client.status == "active")).all():
        ctx = job_context(client.id, "inventory.stale_scan")
        try:
            sources = {s.id: s for s in ctx.db.scalars(select(InventorySource)).all()}
            for v in ctx.db.scalars(select(Vehicle).where(Vehicle.inventory_status.in_(("available", "pending", "unknown")),
                                                          Vehicle.stale.is_(False))).all():
                src = sources.get(v.source_id)
                if src and v.last_seen_at < now - timedelta(hours=src.stale_after_hours):
                    v.stale = True
                    ctx.db.add(VehicleChange(client_id=client.id, vehicle_id=v.id, change_type="stale",
                                             changes={"last_seen_at": v.last_seen_at.isoformat(), "threshold_hours": src.stale_after_hours}))
                    lst = ctx.db.scalar(select(Listing).where(Listing.vehicle_id == v.id))
                    if lst:
                        for pub in ctx.db.scalars(select(ChannelPublication).where(
                                ChannelPublication.listing_id == lst.id,
                                ChannelPublication.status.in_(("published_confirmed", "submitted", "manual_required")))).all():
                            pub.needs_update = True
                            pub.update_reason = "Availability unconfirmed (stale inventory data): confirm the vehicle or withdraw the listing"
                    flagged += 1
            for pub in ctx.db.scalars(select(ChannelPublication).where(ChannelPublication.status == "submitted",
                                                                       ChannelPublication.updated_at < now - timedelta(hours=24))).all():
                pub.last_error = "Provider confirmation overdue (>24h); check the channel"
                overdue += 1
            if flagged or overdue:
                audit_ctx(ctx, "inventory.stale_scan", "client", client.id, stale_flagged=flagged, confirmations_overdue=overdue)
            ctx.db.commit()
        finally:
            ctx.db.close()
    return {"stale_flagged": flagged, "confirmations_overdue": overdue}


HANDLERS = {
    "inventory.sync_source": handle_sync_source,
    "listings.generate": handle_generate,
    "publishing.execute": handle_publish_execute,
    "inventory.stale_scan": handle_stale_scan,
}


def sync_source_schedule(db, source: InventorySource) -> None:
    key = f"inventory.sync:{source.id}"
    if source.enabled and source.schedule_minutes and source.source_type in FEED_TYPES:
        upsert_schedule(db, key, "inventory.sync_source", max(30, source.schedule_minutes * 60), client_id=source.client_id,
                        payload={"source_id": source.id}, queue="inventory")
    else:
        remove_schedule(db, key)


def ensure_platform_schedules(db) -> None:
    upsert_schedule(db, "inventory.stale_scan", "inventory.stale_scan", 3600, queue="inventory")


def enqueue_sync_now(db, source: InventorySource) -> Job:
    job, _ = enqueue(db, "inventory.sync_source", client_id=source.client_id, payload={"source_id": source.id},
                     queue="inventory", idempotency_key=f"sync-now:{source.id}:{utcnow().strftime('%Y%m%d%H%M')}")
    return job

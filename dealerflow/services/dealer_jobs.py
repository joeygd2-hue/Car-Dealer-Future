"""Phase 2 background jobs: feed sync, listing generation, publishing operations, stale scan.

Handlers are registered with ``@jobs.handler`` and run by the shared worker. Each opens a tenant-scoped
session for its client. Scheduling uses time-bucketed job keys, so restarts never double-run a slot.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import utcnow
from ..jobs import enqueue, handler
from ..models import Client
from ..models_dealer import ChannelPublication, InventorySource, Listing, Vehicle, VehicleChange
from ..tenancy import system_context
from .inventory.feeds import FEED_TYPES, sync_feed_source
from .listings.service import generate_listing
from .publishing.service import execute_operation


@handler("inventory_sync")
def run_inventory_sync(db: Session, payload: dict) -> dict:
    ctx = system_context(payload["client_id"])
    try:
        src = ctx.db.get(InventorySource, payload["source_id"])
        if not src or not src.enabled or src.source_type not in FEED_TYPES:
            return {"skipped": "source disabled or not a feed"}
        try:
            run = sync_feed_source(ctx, src, trigger=payload.get("trigger", "schedule"))
        except Exception as exc:
            ctx.db.rollback()
            src = ctx.db.get(InventorySource, payload["source_id"])
            src.last_status, src.last_sync_at = "failed", utcnow()
            audit(ctx.db, "inventory.sync_failed", entity_type="inventory_source", entity_id=src.id,
                  data={"client_id": ctx.client_id, "error": f"{type(exc).__name__}: {exc}"[:500]})
            ctx.db.commit()
            raise
        ctx.db.commit()
        return {"run_id": run.id, "status": run.status}
    finally:
        ctx.close()


@handler("listing_generate")
def run_listing_generate(db: Session, payload: dict) -> dict:
    ctx = system_context(payload["client_id"])
    try:
        lst = ctx.db.get(Listing, payload["listing_id"])
        if not lst or lst.status == "sold_withdrawn":
            return {"skipped": "listing inactive"}
        v = ctx.db.get(Vehicle, lst.vehicle_id)
        if v.validation_status == "invalid" or v.inventory_status in ("sold", "removed"):
            if v.validation_status == "invalid":
                lst.status = "needs_validation"
            ctx.db.commit()
            return {"skipped": "vehicle not listable"}
        rev = generate_listing(ctx, lst.id)
        ctx.db.commit()
        return {"revision_id": rev.id, "errors": len(rev.guardrails["errors"])}
    finally:
        ctx.close()


@handler("publish_operation")
def run_publish_operation(db: Session, payload: dict) -> dict:
    ctx = system_context(payload["client_id"])
    try:
        result = execute_operation(ctx, payload["operation_id"])
        ctx.db.commit()
        return result
    finally:
        ctx.close()


@handler("inventory_stale_scan")
def run_stale_scan(db: Session, payload: dict) -> dict:
    """Flag vehicles whose availability can no longer be confirmed, and overdue provider confirmations."""
    now = utcnow()
    flagged = overdue = 0
    for client_id in db.scalars(select(Client.id).where(Client.status == "active")).all():
        ctx = system_context(client_id)
        try:
            sources = {s.id: s for s in ctx.db.scalars(select(InventorySource))}
            for v in ctx.db.scalars(select(Vehicle).where(Vehicle.inventory_status.in_(("available", "pending", "unknown")),
                                                          Vehicle.stale.is_(False))):
                src = sources.get(v.source_id)
                if src and v.last_seen_at < now - timedelta(hours=src.stale_after_hours):
                    v.stale = True
                    ctx.db.add(VehicleChange(client_id=client_id, vehicle_id=v.id, change_type="stale",
                                             changes={"last_seen_at": v.last_seen_at.isoformat(), "threshold_hours": src.stale_after_hours}))
                    lst = ctx.db.scalar(select(Listing).where(Listing.vehicle_id == v.id))
                    if lst:
                        for pub in ctx.db.scalars(select(ChannelPublication).where(
                                ChannelPublication.listing_id == lst.id,
                                ChannelPublication.status.in_(("published_confirmed", "submitted", "manual_required")))):
                            pub.needs_update = True
                            pub.update_reason = "Availability unconfirmed (stale inventory data): confirm the vehicle or withdraw the listing"
                    flagged += 1
            for pub in ctx.db.scalars(select(ChannelPublication).where(ChannelPublication.status == "submitted",
                                                                       ChannelPublication.updated_at < now - timedelta(hours=24))):
                pub.last_error = "Provider confirmation overdue (more than 24 hours); check the channel"
                overdue += 1
            if flagged or overdue:
                audit(ctx.db, "inventory.stale_scan", entity_type="client", entity_id=client_id,
                      data={"client_id": client_id, "stale_flagged": flagged, "confirmations_overdue": overdue})
            ctx.db.commit()
        finally:
            ctx.close()
    return {"stale_flagged": flagged, "confirmations_overdue": overdue}


def schedule(db: Session, now: datetime | None = None) -> int:
    """Called by the worker every loop: enqueue due feed syncs and the hourly stale scan (idempotent per slot)."""
    now = now or utcnow()
    created = 0
    for src in db.scalars(select(InventorySource).where(InventorySource.enabled.is_(True),
                                                        InventorySource.schedule_minutes.is_not(None),
                                                        InventorySource.source_type.in_(FEED_TYPES))):
        minutes = max(15, src.schedule_minutes or 0)
        slot = int(now.timestamp() // (minutes * 60))
        _, c = enqueue(db, "inventory_sync", f"inventory_sync:{src.id}:{slot}",
                       {"client_id": src.client_id, "source_id": src.id}, max_attempts=3)
        created += int(c)
    _, c = enqueue(db, "inventory_stale_scan", f"inventory_stale_scan:{now.strftime('%Y-%m-%dT%H')}", {}, max_attempts=2)
    return created + int(c)


def sync_now(db: Session, src: InventorySource) -> tuple[object, bool]:
    return enqueue(db, "inventory_sync", f"inventory_sync:{src.id}:manual:{utcnow().strftime('%Y%m%d%H%M')}",
                   {"client_id": src.client_id, "source_id": src.id, "trigger": "manual"}, max_attempts=3)

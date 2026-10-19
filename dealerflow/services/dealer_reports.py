"""Per-dealership inventory/listing metrics.

Only counts what the system can verify. We do not report views, inquiries or
sales attributed to listings: no channel integration currently supplies
reliable data for that (see docs/progress/known-limitations.md).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from ..db import utcnow
from ..integrations.listing_channels import CHANNELS as ADAPTERS
from ..models_dealer import ChannelPublication, ImportRun, InventorySource, Listing, ListingReview, ListingRevision, Vehicle
from ..tenancy import ClientContext


def _count(db, q) -> int:
    return db.scalar(select(func.count()).select_from(q.subquery())) or 0


def dealership_metrics(ctx: ClientContext, days: int = 30) -> dict[str, Any]:
    db = ctx.db
    since = utcnow() - timedelta(days=days)
    by_status = dict(db.execute(select(Vehicle.inventory_status, func.count()).group_by(Vehicle.inventory_status)).all())
    listing_status = dict(db.execute(select(Listing.status, func.count()).group_by(Listing.status)).all())
    pubs = db.execute(select(ChannelPublication.channel, ChannelPublication.status, ChannelPublication.confirmation_type,
                             func.count()).group_by(ChannelPublication.channel, ChannelPublication.status,
                                                    ChannelPublication.confirmation_type)).all()
    api_confirmed = sum(n for ch, st, ct, n in pubs if st == "published_confirmed" and ct == "api"
                        and ADAPTERS.get(ch) and ADAPTERS[ch].mode == "api")
    manual_confirmed = sum(n for ch, st, ct, n in pubs if st == "published_confirmed" and ct == "manual_attested")
    sold_needing_update = _count(db, select(ChannelPublication.id).where(ChannelPublication.status == "withdrawal_required"))
    missing_data = _count(db, select(Vehicle.id).where(Vehicle.inventory_status.in_(("available", "pending")),
                                                       Vehicle.validation_status != "valid"))
    sources = []
    for s in db.scalars(select(InventorySource)).all():
        last = db.get(ImportRun, s.last_run_id) if s.last_run_id else None
        recent = db.execute(select(ImportRun.status, func.count()).where(ImportRun.source_id == s.id, ImportRun.started_at >= since)
                            .group_by(ImportRun.status)).all()
        health = "never_synced" if not last else ("failing" if s.last_status == "failed" else
                                                  "degraded" if s.last_status == "partial" else "healthy")
        if last and s.schedule_minutes and s.last_sync_at < utcnow() - timedelta(minutes=s.schedule_minutes * 3):
            health = "overdue"
        sources.append({"id": s.id, "name": s.name, "type": s.source_type, "enabled": s.enabled, "health": health,
                        "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None, "last_status": s.last_status,
                        "runs_last_period": dict(recent)})
    return {
        "period_days": days,
        "inventory": {
            "total": sum(by_status.values()), "by_status": by_status,
            "imported_in_period": _count(db, select(Vehicle.id).where(Vehicle.first_seen_at >= since)),
            "stale": _count(db, select(Vehicle.id).where(Vehicle.stale.is_(True), Vehicle.inventory_status.in_(("available", "pending")))),
            "with_missing_or_inconsistent_data": missing_data,
        },
        "listings": {
            "by_status": listing_status,
            "generated_in_period": _count(db, select(ListingRevision.listing_id).where(
                ListingRevision.created_at >= since, ListingRevision.origin != "human_edit").distinct()),
            "approved_in_period": _count(db, select(ListingReview.listing_id).where(
                ListingReview.decision == "approved", ListingReview.created_at >= since).distinct()),
        },
        "publishing": {
            "published_confirmed_via_api": api_confirmed,
            "published_confirmed_manually": manual_confirmed,
            "manual_publication_required": sum(n for ch, st, ct, n in pubs if st == "manual_required"),
            "failed": sum(n for ch, st, ct, n in pubs if st == "failed"),
            "sold_or_removed_requiring_withdrawal": sold_needing_update,
            "needing_update": _count(db, select(ChannelPublication.id).where(ChannelPublication.needs_update.is_(True),
                                                                             ChannelPublication.status != "withdrawn")),
            "by_channel": [{"channel": ch, "status": st, "confirmation": ct, "count": n} for ch, st, ct, n in pubs],
        },
        "sync_health": sources,
        "not_measured": ["listing views", "inquiries", "sales attributed to listings"],
    }

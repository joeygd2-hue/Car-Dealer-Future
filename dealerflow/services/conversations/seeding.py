"""Fixture/demo seeding for the customer-response engine (evaluation set, tests, ``demo-conversations``).

Vehicles go through Phase 2's real manual-entry path (normalise, validate, diff). Dealer-approved features are
recorded the way the product records them: as the features in an approved listing revision. Seeded approvals
are labelled ``fixture seed`` and are not a substitute for a person reviewing a listing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import Client, Listing, ListingRevision, Vehicle
from ...tenancy import ClientContext


def set_weekly_hours(client: Client, hours: dict[str, Any] | list[Any]) -> None:
    """``hours``: {"0": ["09:00", "19:00"], ..., "6": None} or a 7-item list (Monday first)."""
    weekly = [hours.get(str(wd)) for wd in range(7)] if isinstance(hours, dict) else list(hours)
    client.business_hours = {**(client.business_hours or {}), "weekly": [list(h) if h else None for h in weekly]}


def add_vehicle(db: Session, client: Client, *, verified_at: datetime | None = None, approved_features: list[str] | None = None,
                data_conflict_note: str | None = None, status: str = "AVAILABLE", **fields: Any) -> Vehicle:
    from ..inventory.importer import upsert_manual_vehicle

    feats = list(approved_features or [])
    data = {k: fields.get(k) for k in ("vin", "stock_number", "year", "make", "model", "trim", "mileage")}
    data["status"] = status.lower()
    data["features"] = feats
    if fields.get("price_cents") is not None:
        data["price"] = f"{fields['price_cents'] / 100:.2f}"
    # Phase 2's importer relies on the session's tenant scope to find this client's manual source.
    db.flush()
    previous = db.info.get("tenant_id")
    db.info["tenant_id"] = client.id
    try:
        v = upsert_manual_vehicle(ClientContext(client=client, db=db, user=None, is_staff=True, role="system"), data)
        v.verified_at, v.data_conflict_note = verified_at, data_conflict_note
        if feats:
            approve_features(db, v, feats)
        db.flush()
    finally:
        if previous is None:
            db.info.pop("tenant_id", None)
        else:
            db.info["tenant_id"] = previous
    return v


def approve_features(db: Session, v: Vehicle, features: list[str]) -> None:
    lst = db.scalar(select(Listing).where(Listing.client_id == v.client_id, Listing.vehicle_id == v.id))
    if lst is None:
        lst = Listing(client_id=v.client_id, vehicle_id=v.id, status="imported")
        db.add(lst)
        db.flush()
    number = 1 + len(list(db.scalars(select(ListingRevision.id).where(ListingRevision.listing_id == lst.id))))
    rev = ListingRevision(client_id=v.client_id, listing_id=lst.id, revision_number=number, origin="human_edit",
                          content={}, facts={"features": list(features)}, facts_hash="fixture-seed",
                          vehicle_version=v.version, settings_version=0, guardrails={"errors": [], "warnings": []},
                          created_by_label="fixture seed")
    db.add(rev)
    db.flush()
    lst.current_revision_id = lst.approved_revision_id = rev.id
    lst.status, lst.approved_at = "approved", utcnow()

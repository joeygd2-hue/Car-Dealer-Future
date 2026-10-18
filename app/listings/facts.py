"""Builds the *verified facts* object for a vehicle. Generators may only use what
is in here: dealer-supplied inventory data plus approved dealer settings."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.time import fingerprint
from app.listings.dealer_settings import ListingSettings
from app.models import Vehicle, VehiclePhoto

FACT_FIELDS = ["year", "make", "model", "trim", "body_style", "mileage", "condition", "exterior_color", "interior_color",
               "transmission", "drivetrain", "fuel_type", "engine", "title_status", "owner_count", "vin", "stock_number"]


def build_facts(db, vehicle: Vehicle, s: ListingSettings) -> dict[str, Any]:
    photos = [p.url for p in db.scalars(select(VehiclePhoto).where(VehiclePhoto.vehicle_id == vehicle.id)
                                        .order_by(VehiclePhoto.position))]
    vehicle_facts = {f: getattr(vehicle, f) for f in FACT_FIELDS}
    if not s.show_vin:
        vehicle_facts["vin"] = None
    if not s.show_stock_number:
        vehicle_facts["stock_number"] = None
    location = {k: getattr(vehicle, f"location_{k}") for k in ("name", "address", "city", "state", "postal")}
    facts = {
        "vehicle": vehicle_facts,
        "price_usd": round(vehicle.price_cents / 100) if vehicle.price_cents else None,
        "features": list(vehicle.features or []),
        "photos": photos,
        "location": location,
        "dealer_description": vehicle.description if s.use_dealer_description else None,
        "inventory_status": vehicle.inventory_status,
        "dealer": {
            "contact": s.contact.model_dump(),
            "business_hours": s.business_hours.model_dump(),
            "financing_statement": s.financing_statement.model_dump(),
            "disclaimers": [d.model_dump() for d in s.disclaimers],
            "call_to_action": s.call_to_action.model_dump(),
            "brand_voice": s.brand_voice,
            "brand_voice_notes": s.brand_voice_notes,
        },
    }
    facts["missing"] = [k for k, v in vehicle_facts.items() if v in (None, "") and k not in ("vin", "stock_number", "owner_count", "title_status")]
    if facts["price_usd"] is None:
        facts["missing"].append("price")
    if not photos:
        facts["missing"].append("photos")
    return facts


def facts_hash(facts: dict[str, Any]) -> str:
    # Inventory status is excluded: a sold car is handled by status, not regeneration.
    return fingerprint({k: v for k, v in facts.items() if k != "inventory_status"})

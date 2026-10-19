"""Build the verified fact sheet the response engine is allowed to use.

Rules: only dealer-approved business info, only inventory fields that are fresh, verified and
not in conflict. Anything else is reported as "needs confirmation".

Phase 2 inventory mapping: ``inventory_status`` available/pending/sold is quoted as AVAILABLE/PENDING/SOLD;
"removed" or "unknown" is never quoted. Phase 2's ``stale`` flag and an ``invalid`` validation status count as a
data conflict. "Approved features" are the features in the vehicle's approved listing revision that are still
in the current inventory record (a dealership person approved them; raw feed features are not used).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import FAQ, ApprovedTemplate, Client, Listing, ListingRevision, ResponseAutomationSettings, Vehicle
from .analysis import Analysis
from .language import strip_accents

DAY_NAMES = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"],
}


@dataclass
class Fact:
    key: str
    value: str
    source: str


@dataclass
class FactSheet:
    dealership_name: str
    language: str
    business_approved: bool
    phone: str | None = None
    address: str | None = None
    hours_text: str | None = None
    hours: dict[int, tuple[str, str] | None] = field(default_factory=dict)
    vehicle: Vehicle | None = None
    vehicle_title: str | None = None
    vehicle_ambiguous: bool = False
    price_text: str | None = None
    availability: str | None = None  # AVAILABLE|PENDING|SOLD when verified, else None
    mileage_text: str | None = None
    features: list[str] = field(default_factory=list)
    inventory_problems: list[str] = field(default_factory=list)  # stale|conflict|unverified|price_missing
    facts: list[Fact] = field(default_factory=list)
    templates: dict[str, ApprovedTemplate] = field(default_factory=dict)
    faqs: list[FAQ] = field(default_factory=list)
    timezone: str = "America/Chicago"

    def allowed_strings(self) -> list[str]:
        out = [f.value for f in self.facts]
        out += [t.body for t in self.templates.values()]
        out += [f.answer for f in self.faqs]
        return out


def fmt_money(cents: int) -> str:
    dollars = cents / 100
    return f"${dollars:,.0f}" if cents % 100 == 0 else f"${dollars:,.2f}"


def fmt_time(hhmm: str, lang: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    if lang == "es":
        return f"{h}:{m:02d}"
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def hours_text(hours: dict[int, tuple[str, str] | None], lang: str) -> str:
    names = DAY_NAMES[lang]
    closed_word = "closed" if lang == "en" else "cerrado"
    parts = []
    for wd in range(7):
        h = hours.get(wd)
        parts.append(f"{names[wd]} {fmt_time(h[0], lang)}–{fmt_time(h[1], lang)}" if h else f"{names[wd]} {closed_word}")
    return ", ".join(parts)


STATUS_MAP = {"available": "AVAILABLE", "pending": "PENDING", "sold": "SOLD"}


def vehicle_title(v: Vehicle) -> str:
    return " ".join(str(p) for p in [v.year, v.make, v.model, v.trim] if p) or "vehicle"


def vehicle_status(v: Vehicle) -> str | None:
    """AVAILABLE | PENDING | SOLD, or None when the inventory record does not say (removed/unknown)."""
    return STATUS_MAP.get(v.inventory_status or "")


def vehicle_conflict(v: Vehicle) -> str | None:
    if v.data_conflict_note:
        return v.data_conflict_note
    if v.stale:
        return "inventory feed has not confirmed this vehicle recently (stale)"
    if v.validation_status == "invalid":
        return "inventory record failed validation"
    return None


def approved_features(db: Session, v: Vehicle) -> list[str]:
    lst = db.scalar(select(Listing).where(Listing.client_id == v.client_id, Listing.vehicle_id == v.id,
                                          Listing.approved_revision_id.is_not(None)))
    if lst is None:
        return []
    rev = db.get(ListingRevision, lst.approved_revision_id)
    if rev is None or rev.client_id != v.client_id:
        return []
    current = {str(f).strip().lower() for f in (v.features or [])}
    return [str(f) for f in ((rev.facts or {}).get("features") or []) if str(f).strip().lower() in current]


def resolve_vehicle(db: Session, dealership_id: int, analysis: Analysis, text: str,
                    current_vehicle_id: int | None) -> tuple[Vehicle | None, bool]:
    """Find the vehicle the customer means. Returns (vehicle, ambiguous)."""
    vehicles = list(db.scalars(select(Vehicle).where(Vehicle.client_id == dealership_id)))
    for vin in analysis.vins:
        for v in vehicles:
            if v.vin and v.vin.upper() == vin:
                return v, False
    for stock in analysis.stock_numbers:
        for v in vehicles:
            if v.stock_number and v.stock_number.upper() == stock:
                return v, False
    norm = strip_accents(text.lower())
    matches = []
    for v in vehicles:
        model = (v.model or "").lower()
        if model and re.search(rf"\b{re.escape(model)}\b", norm):
            make = (v.make or "").lower()
            score = 2 + (1 if make and make in norm else 0) + (1 if v.year and v.year in analysis.years else 0)
            matches.append((score, v))
    if matches:
        matches.sort(key=lambda x: -x[0])
        top = [v for s, v in matches if s == matches[0][0]]
        if len(top) == 1:
            return top[0], False
        if current_vehicle_id and any(v.id == current_vehicle_id for v in top):
            return next(v for v in top if v.id == current_vehicle_id), False
        return None, True
    if current_vehicle_id:
        v = db.get(Vehicle, current_vehicle_id)
        if v and v.client_id == dealership_id:
            return v, False
    return None, False


def build_fact_sheet(db: Session, dealership: Client, settings: ResponseAutomationSettings, language: str,
                     vehicle: Vehicle | None, vehicle_ambiguous: bool = False, now: datetime | None = None) -> FactSheet:
    now = now or utcnow()
    fs = FactSheet(dealership_name=dealership.name, language=language, business_approved=dealership.business_info_approved,
                   timezone=dealership.timezone, vehicle_ambiguous=vehicle_ambiguous)
    fs.facts.append(Fact("dealership_name", dealership.name, "dealership profile"))
    if dealership.business_info_approved:
        if dealership.phone:
            fs.phone = dealership.phone
            fs.facts.append(Fact("phone", dealership.phone, "approved business info"))
        if dealership.full_address:
            fs.address = dealership.full_address
            fs.facts.append(Fact("address", dealership.full_address, "approved business info"))
        if dealership.hours:
            fs.hours = {h.weekday: (None if h.closed or not h.open_time else (h.open_time, h.close_time)) for h in dealership.hours}
            for wd in range(7):
                fs.hours.setdefault(wd, None)
            fs.hours_text = hours_text(fs.hours, language)
            fs.facts.append(Fact("hours", fs.hours_text, "approved business hours"))
    if vehicle is not None:
        fs.vehicle = vehicle
        fs.vehicle_title = vehicle_title(vehicle)
        fs.facts.append(Fact("vehicle", fs.vehicle_title, f"inventory vehicle #{vehicle.id}"))
        if vehicle.year:
            fs.facts.append(Fact("year", str(vehicle.year), f"inventory vehicle #{vehicle.id}"))
        fresh = vehicle.verified_at is not None and now - vehicle.verified_at <= timedelta(hours=settings.stale_inventory_hours)
        conflict = vehicle_conflict(vehicle)
        status = vehicle_status(vehicle)
        if conflict:
            fs.inventory_problems.append("conflict")
        if vehicle.verified_at is None:
            fs.inventory_problems.append("unverified")
        elif not fresh:
            fs.inventory_problems.append("stale")
        trusted = fresh and not conflict
        if trusted and status:
            fs.availability = status
            fs.facts.append(Fact("availability", status, f"inventory verified {vehicle.verified_at:%Y-%m-%d %H:%M} UTC"))
        if trusted and vehicle.price_cents:
            fs.price_text = fmt_money(vehicle.price_cents)
            fs.facts.append(Fact("price", fs.price_text, f"inventory verified {vehicle.verified_at:%Y-%m-%d %H:%M} UTC"))
        elif not vehicle.price_cents:
            fs.inventory_problems.append("price_missing")
        if vehicle.mileage is not None and trusted:
            fs.mileage_text = f"{vehicle.mileage:,}"
            fs.facts.append(Fact("mileage", fs.mileage_text, f"inventory vehicle #{vehicle.id}"))
        fs.features = approved_features(db, vehicle)
        if fs.features:
            fs.facts.append(Fact("features", ", ".join(fs.features), "dealer-approved features"))
        if vehicle.stock_number:
            fs.facts.append(Fact("stock", vehicle.stock_number, f"inventory vehicle #{vehicle.id}"))
    for t in db.scalars(select(ApprovedTemplate).where(
            ApprovedTemplate.client_id == dealership.id, ApprovedTemplate.language == language,
            ApprovedTemplate.active.is_(True), ApprovedTemplate.approved_at.is_not(None))):
        fs.templates[t.intent] = t
    fs.faqs = list(db.scalars(select(FAQ).where(
        FAQ.client_id == dealership.id, FAQ.language == language, FAQ.active.is_(True), FAQ.approved_at.is_not(None))))
    return fs


def match_faq(fs: FactSheet, text: str) -> FAQ | None:
    norm = strip_accents(text.lower())
    best, best_hits = None, 0
    for faq in fs.faqs:
        kws = [strip_accents(k.lower()) for k in (faq.keywords or []) if k.strip()]
        hits = sum(1 for k in kws if re.search(rf"\b{re.escape(k)}\b", norm))
        if kws and hits >= max(1, min(2, len(kws))) and hits > best_hits:
            best, best_hits = faq, hits
    return best

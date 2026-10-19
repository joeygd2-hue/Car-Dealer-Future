"""Build the verified fact sheet the response engine is allowed to use.

Rules: only dealer-approved business info, only inventory fields that are fresh, verified and
not in conflict. Anything else is reported as "needs confirmation".

Inventory verification uses Phase 2's ``Vehicle.last_seen_at`` (set by every feed/file sync that sees the
vehicle and by staff "confirm availability"). A vehicle is trusted only when it was seen within the
dealership's freshness window, is not flagged ``stale``, did not fail validation and has no
``data_conflict_note``. Approved features are the dealer's own feature list (Phase 2 ``features``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models_customer import FAQ, ApprovedResponseTemplate, ResponseAutomationSettings
from ...models_dealer import Client, Vehicle
from .analysis import Analysis
from .language import strip_accents

STATUS_MAP = {"available": "AVAILABLE", "pending": "PENDING", "sold": "SOLD"}  # "removed" -> unknown

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
    templates: dict[str, ApprovedResponseTemplate] = field(default_factory=dict)
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


def vehicle_title(v: Vehicle) -> str:
    return " ".join(str(p) for p in [v.year, v.make, v.model, v.trim] if p) or "vehicle"


def client_address(c: Client) -> str | None:
    line2 = " ".join(x for x in (f"{c.city}," if c.city else None, c.state, c.postal_code) if x)
    parts = [x for x in (c.address_line, line2) if x]
    return ", ".join(parts) or None


def client_hours(c: Client) -> dict[int, tuple[str, str] | None]:
    """Structured weekly hours from ``clients.business_hours``; {} when not configured."""
    raw = c.business_hours or {}
    out: dict[int, tuple[str, str] | None] = {}
    for wd in range(7):
        h = raw.get(str(wd), raw.get(wd)) if isinstance(raw, dict) else None
        if isinstance(h, (list, tuple)) and len(h) == 2 and all(isinstance(x, str) and re.fullmatch(r"\d{1,2}:\d{2}", x) for x in h):
            out[wd] = (h[0], h[1])
        elif str(wd) in raw or wd in raw:
            out[wd] = None
    return out if out else {}


def resolve_vehicle(db: Session, dealership_id: int, analysis: Analysis, text: str,
                    current_vehicle_id: int | None) -> tuple[Vehicle | None, bool]:
    """Find the vehicle the customer means. Returns (vehicle, ambiguous)."""
    vehicles = list(db.scalars(select(Vehicle).where(Vehicle.client_id == dealership_id, Vehicle.removed_at.is_(None))))
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
        address = client_address(dealership)
        if address:
            fs.address = address
            fs.facts.append(Fact("address", address, "approved business info"))
        hours = client_hours(dealership)
        if hours:
            fs.hours = hours
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
        seen = vehicle.last_seen_at
        fresh = (seen is not None and now - seen <= timedelta(hours=settings.stale_inventory_hours)
                 and not vehicle.stale and vehicle.removed_at is None)
        if vehicle.data_conflict_note or vehicle.validation_status == "invalid":
            fs.inventory_problems.append("conflict")
        if seen is None:
            fs.inventory_problems.append("unverified")
        elif not fresh:
            fs.inventory_problems.append("stale")
        trusted = fresh and not vehicle.data_conflict_note and vehicle.validation_status != "invalid"
        status = STATUS_MAP.get(vehicle.inventory_status)
        if trusted and status:
            fs.availability = status
            fs.facts.append(Fact("availability", status, f"inventory verified {seen:%Y-%m-%d %H:%M} UTC"))
        if trusted and vehicle.price_cents:
            fs.price_text = fmt_money(vehicle.price_cents)
            fs.facts.append(Fact("price", fs.price_text, f"inventory verified {seen:%Y-%m-%d %H:%M} UTC"))
        elif not vehicle.price_cents:
            fs.inventory_problems.append("price_missing")
        if vehicle.mileage is not None and trusted:
            fs.mileage_text = f"{vehicle.mileage:,}"
            fs.facts.append(Fact("mileage", fs.mileage_text, f"inventory vehicle #{vehicle.id}"))
        fs.features = [str(f) for f in (vehicle.features or [])]
        if fs.features:
            fs.facts.append(Fact("features", ", ".join(fs.features), "dealer-supplied feature list"))
        if vehicle.stock_number:
            fs.facts.append(Fact("stock", vehicle.stock_number, f"inventory vehicle #{vehicle.id}"))
    for t in db.scalars(select(ApprovedResponseTemplate).where(
            ApprovedResponseTemplate.client_id == dealership.id, ApprovedResponseTemplate.language == language,
            ApprovedResponseTemplate.active.is_(True), ApprovedResponseTemplate.approved_at.is_not(None))):
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

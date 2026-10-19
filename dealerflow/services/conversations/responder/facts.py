"""Build the verified fact sheet the response engine is allowed to use.

Rules: only dealer-approved business info, only inventory fields that are fresh, verified and not in
conflict. Anything else is reported as "needs confirmation".

Phase 2 inventory mapping (Phase 3 used ``verified_at``/``data_conflict_note``):
- fresh   = not ``stale`` and ``last_seen_at`` (set by every feed/upload sighting and by staff "Confirm
            availability") within the dealership's freshness window;
- conflict = a validation issue with severity ``error`` (whole record untrusted), or an issue on the
            ``price``/``mileage`` field (only that field is withheld);
- features = the dealer-supplied ``features`` list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....db import utcnow
from ....models import Client, DealerFAQ, ResponseAutomationSettings, ResponseTemplate
from ....models_dealer import Vehicle
from .analysis import Analysis
from .language import strip_accents

DAY_NAMES = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"],
}
AVAILABILITY = {"available": "AVAILABLE", "pending": "PENDING", "sold": "SOLD"}


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
    templates: dict[str, ResponseTemplate] = field(default_factory=dict)
    faqs: list[DealerFAQ] = field(default_factory=list)
    timezone: str = "America/Chicago"

    def allowed_strings(self) -> list[str]:
        out = [f.value for f in self.facts]
        out += [t.body for t in self.templates.values()]
        out += [f.answer for f in self.faqs]
        return out


def vehicle_title(v: Vehicle) -> str:
    return " ".join(str(x) for x in (v.year, v.make, v.model, v.trim) if x) or f"vehicle #{v.id}"


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


def parse_hours(raw: dict | None) -> dict[int, tuple[str, str] | None]:
    """``clients.business_hours`` → {weekday: (open, close) | None}. Malformed days count as closed."""
    out: dict[int, tuple[str, str] | None] = {}
    for wd in range(7):
        v = (raw or {}).get(str(wd))
        ok = isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, str) and re.fullmatch(r"\d{2}:\d{2}", x) for x in v)
        out[wd] = (v[0], v[1]) if ok and v[0] < v[1] else None
    return out


def full_address(c: Client) -> str | None:
    parts = [c.address_line, c.city, " ".join(p for p in (c.state, c.postal_code) if p)]
    parts = [p for p in parts if p]
    return ", ".join(parts) if parts else None


def field_conflicts(v: Vehicle) -> tuple[bool, set[str]]:
    """(whole record untrusted, fields with conflicting/implausible data)."""
    issues = v.validation_issues or []
    whole = v.validation_status == "invalid" or any(i.get("severity") == "error" and i.get("field") not in ("price", "mileage")
                                                    for i in issues)
    fields = {i.get("field") for i in issues if i.get("field") in ("price", "mileage") and
              (i.get("severity") == "error" or "implausible" in (i.get("code") or "") or "conflict" in (i.get("code") or ""))}
    return whole, fields


def resolve_vehicle(db: Session, client_id: int, analysis: Analysis, text: str,
                    current_vehicle_id: int | None) -> tuple[Vehicle | None, bool]:
    """Find the vehicle the customer means. Returns (vehicle, ambiguous). Only this client's vehicles are considered."""
    vehicles = list(db.scalars(select(Vehicle).where(Vehicle.client_id == client_id,
                                                     Vehicle.inventory_status.in_(("available", "pending", "sold")))))
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
        if v and v.client_id == client_id:
            return v, False
    return None, False


def build_fact_sheet(db: Session, client: Client, settings: ResponseAutomationSettings, language: str,
                     vehicle: Vehicle | None, vehicle_ambiguous: bool = False, now: datetime | None = None) -> FactSheet:
    now = now or utcnow()
    approved = client.business_info_approved_at is not None
    fs = FactSheet(dealership_name=client.name, language=language, business_approved=approved,
                   timezone=client.timezone, vehicle_ambiguous=vehicle_ambiguous)
    fs.facts.append(Fact("dealership_name", client.name, "dealership profile"))
    if approved:
        if client.phone:
            fs.phone = client.phone
            fs.facts.append(Fact("phone", client.phone, "approved business info"))
        addr = full_address(client)
        if addr:
            fs.address = addr
            fs.facts.append(Fact("address", addr, "approved business info"))
        hours = parse_hours(client.business_hours)
        if any(hours.values()):
            fs.hours = hours
            fs.hours_text = hours_text(fs.hours, language)
            fs.facts.append(Fact("hours", fs.hours_text, "approved business hours"))
    if vehicle is not None:
        if vehicle.client_id != client.id:  # defence in depth: never use another dealership's vehicle
            raise ValueError("vehicle belongs to another dealership")
        fs.vehicle = vehicle
        fs.vehicle_title = vehicle_title(vehicle)
        fs.facts.append(Fact("vehicle", fs.vehicle_title, f"inventory vehicle #{vehicle.id}"))
        if vehicle.year:
            fs.facts.append(Fact("year", str(vehicle.year), f"inventory vehicle #{vehicle.id}"))
        seen = vehicle.last_seen_at
        fresh = (not vehicle.stale and seen is not None
                 and now - seen <= timedelta(hours=settings.stale_inventory_hours))
        whole_conflict, conflict_fields = field_conflicts(vehicle)
        if whole_conflict or conflict_fields:
            fs.inventory_problems.append("conflict")
        if seen is None:
            fs.inventory_problems.append("unverified")
        elif not fresh:
            fs.inventory_problems.append("stale")
        trusted = fresh and not whole_conflict
        status = AVAILABILITY.get(vehicle.inventory_status)
        if trusted and status:
            fs.availability = status
            fs.facts.append(Fact("availability", status, f"inventory confirmed {seen:%Y-%m-%d %H:%M} UTC"))
        if trusted and vehicle.price_cents and "price" not in conflict_fields:
            fs.price_text = fmt_money(vehicle.price_cents)
            fs.facts.append(Fact("price", fs.price_text, f"inventory confirmed {seen:%Y-%m-%d %H:%M} UTC"))
        elif not vehicle.price_cents:
            fs.inventory_problems.append("price_missing")
        if vehicle.mileage is not None and trusted and "mileage" not in conflict_fields:
            fs.mileage_text = f"{vehicle.mileage:,}"
            fs.facts.append(Fact("mileage", fs.mileage_text, f"inventory vehicle #{vehicle.id}"))
        fs.features = [str(f) for f in (vehicle.features or [])]
        if fs.features:
            fs.facts.append(Fact("features", ", ".join(fs.features), "dealer-supplied features"))
        if vehicle.stock_number:
            fs.facts.append(Fact("stock", vehicle.stock_number, f"inventory vehicle #{vehicle.id}"))
    for t in db.scalars(select(ResponseTemplate).where(
            ResponseTemplate.client_id == client.id, ResponseTemplate.language == language,
            ResponseTemplate.active.is_(True), ResponseTemplate.approved_at.is_not(None))):
        fs.templates[t.intent] = t
    fs.faqs = list(db.scalars(select(DealerFAQ).where(
        DealerFAQ.client_id == client.id, DealerFAQ.language == language, DealerFAQ.active.is_(True),
        DealerFAQ.approved_at.is_not(None))))
    return fs


def match_faq(fs: FactSheet, text: str) -> DealerFAQ | None:
    norm = strip_accents(text.lower())
    best, best_hits = None, 0
    for faq in fs.faqs:
        kws = [strip_accents(k.lower()) for k in (faq.keywords or []) if k.strip()]
        hits = sum(1 for k in kws if re.search(rf"\b{re.escape(k)}\b", norm))
        if kws and hits >= max(1, min(2, len(kws))) and hits > best_hits:
            best, best_hits = faq, hits
    return best

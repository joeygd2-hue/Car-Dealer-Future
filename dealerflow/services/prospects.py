"""Lead intake: validation, deduplication across sources, merging and evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import utcnow
from ..models import DEALERSHIP_TYPES, Evidence, Prospect, ProspectSource
from .crm import add_activity
from .normalize import domain_of, is_free_mail_domain, normalize_name, validate_prospect_fields

MERGE_FIELDS = ("website", "inventory_url", "address_line", "city", "state", "postal_code", "lat", "lng",
                "phone_e164", "email", "dealership_type")


@dataclass
class LeadRecord:
    """A dealership as reported by one source. Only fields the source actually provided are set."""

    name: str
    source: str
    source_url: str | None = None
    external_id: str | None = None
    website: str | None = None
    inventory_url: str | None = None
    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    lat: float | None = None
    lng: float | None = None
    phone: str | None = None
    email: str | None = None
    dealership_type: str | None = None
    notes: str = ""
    raw: dict[str, Any] | None = None
    simulated: bool = False
    signals: list[tuple[str, str, str]] = field(default_factory=list)  # (field, value, note) extra evidence

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("name", "website", "inventory_url", "address_line", "city", "state",
                                                "postal_code", "lat", "lng", "phone", "email", "dealership_type")}


@dataclass
class IntakeResult:
    prospect: Prospect | None
    created: bool
    matched_by: str | None
    errors: list[str]
    conflicts: list[str] = field(default_factory=list)


def miles_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_duplicate(db: Session, data: dict[str, Any], *, source: str | None = None,
                   external_id: str | None = None) -> tuple[Prospect | None, str | None]:
    live = Prospect.merged_into_id.is_(None)
    if source and external_id:
        link = db.scalar(select(ProspectSource).where(ProspectSource.source == source,
                                                      ProspectSource.external_id == external_id))
        if link:
            return link.prospect, "source_id"
    domain = domain_of(data.get("website"))
    if domain and not is_free_mail_domain(domain):
        p = db.scalar(select(Prospect).where(live, Prospect.domain == domain))
        if p:
            return p, "domain"
    if data.get("phone_e164"):
        p = db.scalar(select(Prospect).where(live, Prospect.phone_e164 == data["phone_e164"]))
        if p:
            return p, "phone"
    email_domain = domain_of(data.get("email"))
    if email_domain and not is_free_mail_domain(email_domain):
        p = db.scalar(select(Prospect).where(live, Prospect.domain == email_domain))
        if p:
            return p, "email_domain"
    norm = normalize_name(data.get("name") or "")
    if not norm:
        return None, None
    conds = []
    if data.get("postal_code"):
        conds.append(Prospect.postal_code == data["postal_code"])
    if data.get("city"):
        conds.append(Prospect.city.ilike(data["city"]))
    if data.get("lat") is not None:
        conds.append(Prospect.lat.between(data["lat"] - 0.01, data["lat"] + 0.01))
    if not conds:
        conds.append(Prospect.normalized_name == norm)
    for p in db.scalars(select(Prospect).where(live, or_(*conds))):
        ratio = SequenceMatcher(None, norm, p.normalized_name).ratio()
        if ratio >= 0.88:
            if data.get("lat") is not None and p.lat is not None:
                if miles_between(data["lat"], data["lng"], p.lat, p.lng) > 0.5:
                    continue
            return p, f"name~{ratio:.2f}"
    return None, None


def _evidence(db: Session, prospect: Prospect, fld: str, value: Any, source: str, url: str | None, *,
              simulated: bool, note: str = "", verified: bool = False) -> None:
    db.add(Evidence(prospect=prospect, field=fld, value="" if value is None else str(value), source_type=source,
                    source_url=url, collected_at=utcnow(), simulated=simulated, note=note, verified=verified))


def intake(db: Session, rec: LeadRecord, *, actor_id: int | None = None) -> IntakeResult:
    data, errors = validate_prospect_fields(rec.as_dict())
    if not data.get("name"):
        return IntakeResult(None, False, None, errors)
    if data.get("dealership_type") and data["dealership_type"] not in DEALERSHIP_TYPES:
        errors.append(f"unknown dealership_type {data['dealership_type']!r}")
        data["dealership_type"] = None
    if data.get("lat") is not None and data.get("lng") is None:
        data["lat"] = None

    existing, matched_by = find_duplicate(db, data, source=rec.source, external_id=rec.external_id)
    now = utcnow()
    conflicts: list[str] = []
    if existing is None:
        p = Prospect(
            name=data["name"], normalized_name=normalize_name(data["name"]),
            dealership_type=data.get("dealership_type") or "unknown",
            website=data.get("website"), domain=domain_of(data.get("website")),
            inventory_url=data.get("inventory_url"), address_line=data.get("address_line"), city=data.get("city"),
            state=data.get("state"), postal_code=data.get("postal_code"), lat=data.get("lat"), lng=data.get("lng"),
            phone=data.get("phone"), phone_e164=data.get("phone_e164"), email=data.get("email"),
            lead_source=rec.source, discovered_at=now, last_verified_at=now if rec.source != "manual" else None,
            research_notes=rec.notes, simulated=rec.simulated, stage="new",
        )
        if not p.domain and p.email and not is_free_mail_domain(domain_of(p.email)):
            p.domain = domain_of(p.email)
        db.add(p)
        db.flush()
        for fld in ("name",) + MERGE_FIELDS:
            if data.get(fld) not in (None, ""):
                _evidence(db, p, fld, data[fld], rec.source, rec.source_url, simulated=rec.simulated)
        add_activity(db, p, "research", f"Discovered via {rec.source}" + (" [SIMULATED]" if rec.simulated else ""),
                     body=rec.notes, actor_id=actor_id, data={"source_url": rec.source_url})
        audit(db, "prospect.create", actor_id=actor_id, entity_type="prospect", entity_id=p.id,
              data={"source": rec.source, "simulated": rec.simulated})
        created = True
    else:
        p = existing
        created = False
        filled = []
        for fld in MERGE_FIELDS:
            new = data.get(fld)
            if new in (None, ""):
                continue
            cur = getattr(p, fld)
            if fld == "dealership_type" and cur == "unknown":
                cur = None
            if cur in (None, ""):
                setattr(p, fld, new)
                if fld == "phone_e164":
                    p.phone = data.get("phone")
                if fld == "website":
                    p.domain = p.domain or domain_of(new)
                filled.append(fld)
                _evidence(db, p, fld, new, rec.source, rec.source_url, simulated=rec.simulated)
            elif str(cur).strip().lower() != str(new).strip().lower() and fld not in ("lat", "lng"):
                conflicts.append(f"{fld}: kept {cur!r}, {rec.source} says {new!r}")
                _evidence(db, p, fld, new, rec.source, rec.source_url, simulated=rec.simulated,
                          note=f"Conflicts with stored value {cur!r}; not applied")
        if rec.source != "manual":
            p.last_verified_at = now
        add_activity(db, p, "research",
                     f"Matched existing record from {rec.source} ({matched_by})" + (f"; filled {', '.join(filled)}" if filled else ""),
                     body="\n".join(conflicts), actor_id=actor_id, data={"source_url": rec.source_url})
    for fld, value, note in rec.signals:
        _evidence(db, p, fld, value, rec.source, rec.source_url, simulated=rec.simulated, note=note)
    if rec.external_id:
        link = db.scalar(select(ProspectSource).where(ProspectSource.source == rec.source,
                                                      ProspectSource.external_id == rec.external_id))
        if link is None:
            db.add(ProspectSource(prospect=p, source=rec.source, external_id=rec.external_id,
                                  source_url=rec.source_url, raw=rec.raw, fetched_at=now))
        else:
            link.fetched_at, link.raw = now, rec.raw
    db.flush()
    return IntakeResult(p, created, matched_by, errors, conflicts)


def merge_prospects(db: Session, keep: Prospect, drop: Prospect, *, actor_id: int | None) -> None:
    """Manually merge a duplicate the automatic matcher missed. History moves to ``keep``."""
    if keep.id == drop.id:
        raise ValueError("cannot merge a prospect into itself")
    for fld in MERGE_FIELDS + ("phone",):
        if getattr(keep, fld) in (None, "", "unknown") and getattr(drop, fld) not in (None, "", "unknown"):
            setattr(keep, fld, getattr(drop, fld))
    keep.domain = keep.domain or drop.domain
    keep.do_not_contact = keep.do_not_contact or drop.do_not_contact
    if keep.do_not_contact:
        keep.stage = "do_not_contact"
    for coll in ("contacts", "evidence", "sources", "activities"):
        for item in list(getattr(drop, coll)):
            item.prospect_id = keep.id
    from ..models import Call, InboundMessage, Meeting, OutreachMessage, Proposal, Task
    for model in (OutreachMessage, InboundMessage, Meeting, Proposal, Call, Task):
        for row in db.scalars(select(model).where(model.prospect_id == drop.id)):
            row.prospect_id = keep.id
    drop.merged_into_id = keep.id
    add_activity(db, keep, "system", f"Merged duplicate #{drop.id} ({drop.name})", actor_id=actor_id)
    audit(db, "prospect.merge", actor_id=actor_id, entity_type="prospect", entity_id=keep.id, data={"dropped": drop.id})
    db.flush()
    db.expire(keep)

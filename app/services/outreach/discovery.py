"""Prospect discovery (permitted sources only) + dedupe + qualification."""

from __future__ import annotations

import csv
import io
import re
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.integrations.base import ProspectCandidate
from app.models import CostEntry, Dealership, Prospect, ProspectFact
from app.services import audit
from app.services.outreach.compliance import is_suppressed, normalize_domain, normalize_email, normalize_phone

FRANCHISE_BRANDS = ["toyota", "honda", "ford", "chevrolet", "chevy", "nissan", "hyundai", "kia", "bmw", "mercedes",
                    "lexus", "acura", "infiniti", "audi", "volkswagen", "subaru", "mazda", "jeep", "dodge", "ram",
                    "chrysler", "gmc", "buick", "cadillac", "lincoln", "volvo", "porsche", "tesla", "mitsubishi"]
GROUP_MARKERS = ["carmax", "carvana", "autonation", "group 1", "sonic automotive", "vroom", "drivetime"]


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def find_duplicate(db: Session, c: ProspectCandidate) -> Prospect | None:
    conds = []
    if c.source_ref:
        conds.append((Prospect.source == c.source) & (Prospect.source_ref == c.source_ref))
    if c.website and normalize_domain(c.website):
        conds.append(Prospect.domain == normalize_domain(c.website))
    if c.email and normalize_email(c.email):
        conds.append(Prospect.email == normalize_email(c.email))
    if not conds:
        conds.append((Prospect.normalized_name == normalize_name(c.name)) & (Prospect.city == c.city))
    found = db.scalar(select(Prospect).where(or_(*conds)).limit(1))
    if found:
        return found
    phone = normalize_phone(c.phone)
    if phone:
        for p in db.scalars(select(Prospect).where(Prospect.phone.is_not(None))):
            if normalize_phone(p.phone) == phone:
                return p
    return None


def add_candidate(db: Session, c: ProspectCandidate) -> tuple[Prospect, bool]:
    dup = find_duplicate(db, c)
    if dup:
        return dup, False
    expires = utcnow() + timedelta(days=c.content_ttl_days) if c.content_ttl_days else None
    p = Prospect(name=c.name[:200], normalized_name=normalize_name(c.name), address=c.address, city=c.city, state=c.state,
                 postal_code=c.postal_code, phone=c.phone, email=normalize_email(c.email), website=c.website,
                 domain=normalize_domain(c.website) or (normalize_email(c.email) or "@").split("@")[1] or None,
                 source=c.source, source_ref=c.source_ref, source_url=c.source_url, source_content_expires_at=expires,
                 is_simulated=c.simulated, status="NEW")
    db.add(p)
    db.flush()
    for key, value in (("business_name", c.name), ("address", c.address), ("phone", c.phone), ("website", c.website),
                       ("email", c.email), ("categories", ", ".join(c.categories) if c.categories else None),
                       ("business_status", c.business_status)):
        if value:
            db.add(ProspectFact(prospect_id=p.id, key=key, value=str(value), source_name=c.source,
                                source_url=c.source_url, confidence="SOURCE_REPORTED", expires_at=expires))
    return p, True


def discover(db: Session, source, cities: list[str], limit: int, *, run_ref: str) -> dict:
    created, seen, calls = 0, 0, 0
    for city in cities:
        if created >= limit:
            break
        candidates = source.search(city=city, limit=min(20, limit * 2))
        calls += 1
        db.add(CostEntry(scope="OUTREACH", provider=source.name, operation="prospect_search",
                         amount_cents=source.estimated_cost_cents_per_call, is_estimate=True,
                         simulated=source.simulated, reference=run_ref))
        for c in candidates:
            seen += 1
            _, new = add_candidate(db, c)
            if new:
                created += 1
            if created >= limit:
                break
    audit.record(db, "outreach.discovery", actor_type="AGENT", source=source.name, created=created, seen=seen,
                 calls=calls, simulated=source.simulated, external=not source.simulated)
    return {"created": created, "seen": seen, "calls": calls}


def import_csv(db: Session, text: str, *, user_id: int, source_label: str = "CSV") -> dict:
    """Operator-researched prospects. Required columns: name, city; recommended: email, website, phone, source_url."""
    reader = csv.DictReader(io.StringIO(text))
    created = dup = invalid = 0
    for row in reader:
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not row.get("name") or not row.get("source_url"):
            invalid += 1
            continue
        c = ProspectCandidate(name=row["name"], source=source_label, source_ref=None, address=row.get("address") or None,
                              city=row.get("city") or None, state=row.get("state") or "TX",
                              postal_code=row.get("postal_code") or None, phone=row.get("phone") or None,
                              website=row.get("website") or None, email=row.get("email") or None,
                              source_url=row["source_url"])
        p, new = add_candidate(db, c)
        if new:
            created += 1
            if row.get("email_verified", "").lower() in ("yes", "true", "1"):
                p.email_verified_by_human = True
                for f in p.facts:
                    if f.key == "email":
                        f.confidence = "VERIFIED"
            if row.get("contact_name"):
                p.contact_name = row["contact_name"]
                db.add(ProspectFact(prospect_id=p.id, key="contact_name", value=row["contact_name"], source_name=source_label,
                                    source_url=row["source_url"], confidence="SOURCE_REPORTED"))
        else:
            dup += 1
    audit.record(db, "outreach.csv_import", actor_type="USER", actor_user_id=user_id, created=created, duplicates=dup,
                 invalid=invalid)
    return {"created": created, "duplicates": dup, "invalid": invalid}


def qualify(db: Session, p: Prospect, target_cities: list[str]) -> Prospect:
    """Deterministic eligibility. Uncertain cases go to NEEDS_REVIEW instead of being guessed."""
    name = p.name.lower()
    reasons_dq, reasons_review = [], []
    if any(re.search(rf"\b{b}\b", name) for b in FRANCHISE_BRANDS):
        reasons_dq.append("appears to be a franchise (new-car brand in name)")
    if any(g in name for g in GROUP_MARKERS):
        reasons_dq.append("large dealer group / national chain")
    status_fact = next((f.value for f in p.facts if f.key == "business_status"), None)
    if status_fact and status_fact not in ("OPERATIONAL",):
        reasons_dq.append(f"business status {status_fact}")
    if p.converted_dealership_id or db.scalar(select(Dealership.id).where(Dealership.name == p.name)):
        reasons_dq.append("already a client")
    if p.city and target_cities and p.city not in target_cities:
        reasons_review.append(f"city {p.city} outside target area")
    if not p.email:
        reasons_review.append("no published business email on record")
    supp = is_suppressed(db, email=p.email, domain=p.domain, phone=p.phone)
    if supp:
        reasons_dq.append(supp)
    score = 50
    score += 20 if p.city in ("Sugar Land", "Stafford", "Missouri City") else 10 if p.city in target_cities else 0
    score += 15 if p.website else 0
    score += 15 if p.email else 0
    p.fit_score = score
    if reasons_dq:
        p.status, p.status_reason = ("DO_NOT_CONTACT" if supp else "DISQUALIFIED"), "; ".join(reasons_dq)
    elif reasons_review:
        p.status, p.status_reason = "NEEDS_REVIEW", "; ".join(reasons_review)
    else:
        p.status, p.status_reason = "QUALIFIED", None
    audit.record(db, "outreach.prospect_qualified", actor_type="AGENT", entity_type="prospect", entity_id=p.id,
                 status=p.status, reason=p.status_reason, score=score)
    return p


def purge_expired_source_content(db: Session) -> int:
    """Clear provider content whose storage period expired (e.g. Google Places, 30 days), keeping place_id."""
    now = utcnow()
    n = 0
    for f in db.scalars(select(ProspectFact).where(ProspectFact.expires_at.is_not(None), ProspectFact.expires_at < now,
                                                   ProspectFact.confidence != "VERIFIED")):
        db.delete(f)
        n += 1
    for p in db.scalars(select(Prospect).where(Prospect.source_content_expires_at.is_not(None),
                                               Prospect.source_content_expires_at < now)):
        if not p.email_verified_by_human:
            p.address = p.phone = None
        p.source_content_expires_at = None
        n += 1
    return n

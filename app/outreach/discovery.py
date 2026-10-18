"""Prospect discovery, de-duplication, enrichment and qualification."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.time import utcnow
from app.models import Client, CostEntry, PlatformIntegration, Prospect, ProspectContact, ProspectObservation
from app.outreach.policy import is_suppressed
from app.outreach.providers.base import Candidate
from app.outreach.providers import website as web

FRANCHISE_BRANDS = ["toyota", "honda", "ford", "chevrolet", "chevy", "nissan", "hyundai", "kia", "bmw", "mercedes", "lexus",
                    "acura", "infiniti", "mazda", "subaru", "volkswagen", "audi", "jeep", "dodge", "chrysler", "ram", "gmc",
                    "buick", "cadillac", "lincoln", "volvo", "porsche", "tesla", "genesis", "mitsubishi"]
INDEPENDENT_HINTS = ["auto sales", "used", "pre-owned", "preowned", "motors", "auto", "cars", "car co", "outlet", "exchange", "mart"]
DEFAULT_CITIES = ["Sugar Land", "Stafford", "Missouri City", "Richmond", "Rosenberg", "Houston", "Bellaire", "Katy", "Pearland"]


def normalize_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    n = re.sub(r"\b(llc|inc|co|corp|ltd|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    d = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    return d.removeprefix("www.") or None


def dedupe_key(name: str, website: str | None, postal: str | None) -> str:
    d = domain_of(website)
    return f"domain:{d}" if d else f"name:{normalize_name(name)}|{postal or ''}"


def upsert_candidate(db: Session, c: Candidate) -> tuple[Prospect, bool]:
    key = dedupe_key(c.business_name, c.website, c.postal_code)
    p = db.scalar(select(Prospect).where(Prospect.dedupe_key == key))
    if not p and c.source_ref:
        p = db.scalar(select(Prospect).where(Prospect.source == c.source, Prospect.source_ref == c.source_ref))
    if p:
        return p, False
    p = Prospect(business_name=c.business_name, normalized_name=normalize_name(c.business_name), website=c.website,
                 domain=domain_of(c.website), phone=c.phone, address=c.address, city=c.city, state=c.state,
                 postal_code=c.postal_code, source=c.source, source_ref=c.source_ref,
                 source_detail={"types": c.types, "business_status": c.business_status, **c.detail},
                 dedupe_key=key, is_simulated=c.simulated, status="new")
    db.add(p)
    db.flush()
    return p, True


def qualify(db: Session, p: Prospect, criteria: dict[str, Any] | None = None) -> Prospect:
    criteria = criteria or {}
    cities = [x.lower() for x in criteria.get("cities", DEFAULT_CITIES)]
    name = p.business_name.lower()
    results: dict[str, Any] = {}
    reasons = []
    results["operational"] = (p.source_detail or {}).get("business_status") in (None, "OPERATIONAL")
    franchise = any(re.search(rf"\b{b}\b", name) for b in FRANCHISE_BRANDS) and not any(h in name for h in ("used", "pre-owned", "auto sales"))
    results["independent"] = not franchise
    results["in_target_area"] = (p.city or "").lower() in cities and (p.state or "TX").upper() in ("TX", "TEXAS")
    results["has_website"] = bool(p.website)
    results["not_existing_client"] = not (p.converted_client_id or db.scalar(select(Client.id).where(Client.name == p.business_name)))
    contacts = db.scalars(select(ProspectContact).where(ProspectContact.prospect_id == p.id)).all()
    usable = [c for c in contacts if c.email and c.evidence_url and not is_suppressed(db, c.email)]
    results["has_published_email"] = bool(usable)
    results["domain_not_suppressed"] = not (p.domain and is_suppressed(db, f"x@{p.domain}"))
    for k in ("operational", "independent", "in_target_area", "has_website", "not_existing_client", "domain_not_suppressed"):
        if not results[k]:
            reasons.append(k)
    obs = db.scalars(select(ProspectObservation).where(ProspectObservation.prospect_id == p.id)).all()
    score = 0.0
    score += 30 if results["has_published_email"] else 0
    score += 20 if (p.city or "").lower() == "sugar land" else 10 if results["in_target_area"] else 0
    score += 15 if any(o.kind == "spanish_service" for o in obs) else 0
    score += 10 if any(o.kind == "has_inventory_page" for o in obs) else 0
    score += 5 if any(o.kind == "facebook_link" for o in obs) else 0
    p.qualification, p.priority_score = results, score
    if p.status in ("new", "researching", "qualified", "disqualified"):
        if reasons:
            p.status, p.disqualify_reason = "disqualified", ", ".join(reasons)
        elif not results["has_published_email"]:
            p.status, p.disqualify_reason = "researching", "no published business email found yet"
        else:
            p.status, p.disqualify_reason = "qualified", None
    return p


def enrich(db: Session, p: Prospect) -> dict[str, Any]:
    if not p.website:
        p.last_enriched_at = utcnow()
        return {"skipped": "no website"}
    pages = web.fetch_pages(p.website)
    findings, contacts = web.analyze(p.website, pages)
    existing_obs = {o.kind for o in db.scalars(select(ProspectObservation).where(ProspectObservation.prospect_id == p.id))}
    for f in findings:
        if f.kind not in existing_obs:
            db.add(ProspectObservation(prospect_id=p.id, kind=f.kind, statement=f.statement, evidence_url=f.url,
                                       evidence_excerpt=f.excerpt[:500], source="website"))
    existing_emails = {c.email for c in db.scalars(select(ProspectContact).where(ProspectContact.prospect_id == p.id))}
    for c in contacts:
        if c.email not in existing_emails:
            db.add(ProspectContact(prospect_id=p.id, email=c.email, evidence_url=c.url, evidence_excerpt=c.excerpt[:500],
                                   email_basis="published_on_business_website"))
    p.last_enriched_at = utcnow()
    db.flush()
    qualify(db, p)
    audit(db, action="prospect.enriched", entity_type="prospect", entity_id=p.id, actor_type="agent",
          details={"pages": len(pages), "observations": len(findings), "contacts": len(contacts), "status": p.status})
    return {"pages": len(pages), "observations": len(findings), "contacts": len(contacts), "status": p.status}


def run_discovery(db: Session, integration: PlatformIntegration, provider, query: str, limit: int = 20,
                  campaign_id: str | None = None) -> dict[str, Any]:
    candidates = provider.search(query, limit)
    created = 0
    for c in candidates:
        _, new = upsert_candidate(db, c)
        created += int(new)
    if provider.estimated_cost_per_call:
        db.add(CostEntry(category="places", provider=provider.name, units=1, unit_label="search", campaign_id=campaign_id,
                         amount_usd=provider.estimated_cost_per_call, measured=False, ref=query[:120]))
    audit(db, action="prospect.discovery", entity_type="platform_integration", entity_id=integration.id, actor_type="agent",
          details={"query": query, "results": len(candidates), "created": created, "provider": provider.name})
    return {"results": len(candidates), "created": created}

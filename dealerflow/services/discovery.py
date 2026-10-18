"""Running discovery sources and website enrichment."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit, track_integration
from ..db import utcnow
from ..integrations import discovery as sources
from ..integrations.website import research_site
from ..jobs import handler
from ..models import Contact, Evidence, Prospect
from . import costs
from .crm import add_activity, set_stage
from .normalize import domain_of, normalize_email
from .prospects import intake


STORABLE_SOURCES = ("fixture", "osm")
PLACES_REFUSAL = ("Google Places results can't be stored as leads: Google's Places API policies allow storing only "
                  "place IDs, not names, addresses or phone numbers. Use the TxDMV dealer list (CSV import) or "
                  "OpenStreetMap instead. See docs/research/feasibility-report.md.")


def run_source(db: Session, source: str, *, query: str | None = None, actor_id: int | None = None) -> dict:
    if source == "google_places":
        raise ValueError(PLACES_REFUSAL)
    if source not in STORABLE_SOURCES:
        raise ValueError(f"Unknown source {source!r}")
    simulated = source == "fixture"
    with track_integration(db, source, "search", simulated=simulated) as ctx:
        records = sources.SOURCES[source]()
        ctx["detail"] = f"{len(records)} result(s) for {source}"
    costs.record(db, f"discovery:{source}", costs.estimate(db, f"discovery:{source}"), estimate=True,
                 lead_source=source, note="estimated cost per discovery run")
    created = merged = skipped = 0
    for rec in records:
        res = intake(db, rec, actor_id=actor_id)
        if res.prospect is None:
            skipped += 1
        elif res.created:
            created += 1
        else:
            merged += 1
    audit(db, "discovery.run", actor_id=actor_id, data={"source": source, "query": query, "found": len(records),
                                                        "created": created, "merged": merged, "skipped": skipped})
    return {"found": len(records), "created": created, "merged": merged, "skipped": skipped, "simulated": simulated}


@handler("discovery")
def discovery_job(db: Session, payload: dict) -> dict:
    return run_source(db, payload["source"], query=payload.get("query"), actor_id=payload.get("actor_id"))


def enrich_from_website(db: Session, p: Prospect, *, actor_id: int | None = None) -> dict:
    if not p.website:
        return {"skipped": "no website"}
    if p.simulated:
        return {"skipped": "simulated prospect (fixture domains do not resolve)"}
    with track_integration(db, "website", "research") as ctx:
        obs = research_site(p.website)
        ctx["detail"] = f"{p.website}: {len(obs.pages_fetched)} page(s)"
    now = utcnow()
    if obs.blocked_by_robots:
        add_activity(db, p, "research", "Website research skipped: robots.txt disallows it", actor_id=actor_id)
        db.add(Evidence(prospect=p, field="signal:robots_blocked", value="true", source_type="website",
                        source_url=p.website, collected_at=now))
        return {"blocked_by_robots": True}
    for fld, value, note in obs.signals:
        db.add(Evidence(prospect=p, field=fld, value=value, source_type="website", source_url=obs.final_url,
                        collected_at=now, note=note))
    if obs.inventory_url and not p.inventory_url:
        p.inventory_url = obs.inventory_url
        db.add(Evidence(prospect=p, field="inventory_url", value=obs.inventory_url, source_type="website",
                        source_url=obs.final_url, collected_at=now, note="Link found on homepage"))
    site_domain = domain_of(obs.final_url)
    for e in obs.public_emails:
        e = normalize_email(e)
        if not e:
            continue
        db.add(Evidence(prospect=p, field="email", value=e, source_type="website", source_url=obs.final_url,
                        collected_at=now, note="Published mailto link on the dealer's own website"))
        if not p.email and domain_of(e) == site_domain:
            p.email = e
    p.last_verified_at = now
    add_activity(db, p, "research", f"Website researched ({len(obs.pages_fetched)} page(s))", actor_id=actor_id,
                 data={"pages": obs.pages_fetched})
    return {"signals": len(obs.signals), "emails": len(obs.public_emails)}


@handler("enrich_website")
def enrich_job(db: Session, payload: dict) -> dict:
    p = db.get(Prospect, payload["prospect_id"])
    if not p:
        return {"skipped": "missing"}
    return enrich_from_website(db, p, actor_id=payload.get("actor_id"))


def add_verified_contact(db: Session, p: Prospect, *, name: str, role: str, email: str | None, phone: str | None,
                         source_url: str, actor_id: int) -> Contact:
    """A person records a contact they verified from a cited public source (or direct conversation)."""
    if not source_url:
        raise ValueError("A source (URL or 'conversation on DATE') is required to mark a contact verified")
    c = Contact(prospect=p, name=name.strip(), role=role.strip(), email=normalize_email(email), phone=phone,
                verified=True, source_url=source_url, verified_at=utcnow(), is_primary=not p.contacts)
    db.add(c)
    add_activity(db, p, "research", f"Verified contact added: {name} ({role})", actor_id=actor_id)
    audit(db, "contact.add", actor_id=actor_id, entity_type="prospect", entity_id=p.id, data={"source": source_url})
    if p.stage == "new":
        set_stage(db, p, "researching", reason="Contact research", workflow=True)
    return c

"""Outreach worker handlers and recurring schedules."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.core.audit import audit
from app.core.jobs import get_flag, upsert_schedule
from app.core.time import utcnow
from app.core.worker import NonRetryableError
from app.models import Job, PlatformIntegration, Prospect
from app.outreach import agent, discovery, policy
from app.outreach.providers import build as build_provider, is_live
from app.outreach.providers.base import ProviderError


def _stopped(db) -> bool:
    return bool(get_flag(db, policy.EMERGENCY_STOP, False) or get_flag(db, policy.GLOBAL_PAUSE, False))


def handle_tick(db, job: Job, payload: dict) -> dict:
    run = agent.tick(db, job_id=job.id)
    return {"run_id": run.id, "state": run.state, "stats": run.stats}


def handle_discover(db, job: Job, payload: dict) -> dict:
    if _stopped(db):
        return {"skipped": "agent paused/stopped"}
    integ = db.get(PlatformIntegration, payload["integration_id"])
    if not integ or integ.kind != "discovery":
        raise NonRetryableError("Discovery integration not found")
    if is_live(integ) and not (integ.live_authorized and settings.outreach_live_allowed):
        raise NonRetryableError("Discovery integration is not authorised for live use")
    try:
        return discovery.run_discovery(db, integ, build_provider(db, integ), payload["query"], campaign_id=payload.get("campaign_id"))
    except ProviderError as exc:
        integ.health_status, integ.health_detail, integ.health_checked_at = "degraded", str(exc)[:300], utcnow()
        db.commit()
        if not exc.retryable:
            raise NonRetryableError(str(exc)) from exc
        raise


def handle_enrich(db, job: Job, payload: dict) -> dict:
    if _stopped(db):
        return {"skipped": "agent paused/stopped"}
    p = db.get(Prospect, payload["prospect_id"])
    if not p or p.status not in ("new", "researching"):
        return {"skipped": "not enrichable"}
    p.status = "researching"
    return discovery.enrich(db, p)


def handle_health(db, job: Job, payload: dict) -> dict:
    out = {}
    for integ in db.scalars(select(PlatformIntegration)).all():
        if is_live(integ) and not integ.live_authorized:
            integ.health_status, integ.health_detail = "unknown", "Not authorised for live use; not checked"
            continue
        try:
            status, detail = build_provider(db, integ).health_check()
        except Exception as exc:  # noqa: BLE001
            status, detail = "down", f"{type(exc).__name__}: {exc}"
        integ.health_status, integ.health_detail, integ.health_checked_at = status, detail[:300], utcnow()
        out[integ.name] = status
    return out


def handle_places_purge(db, job: Job, payload: dict) -> dict:
    """Places ToS: only place_id may be stored indefinitely. Clear cached Places content after 30 days."""
    cutoff = utcnow() - timedelta(days=30)
    n = 0
    for p in db.scalars(select(Prospect).where(Prospect.source == "google_places")).all():
        fetched = (p.source_detail or {}).get("places_fetched_at")
        if fetched and fetched < cutoff.isoformat():
            p.source_detail = {"purged_at": utcnow().isoformat()}
            if not p.last_enriched_at:  # not independently verified from the dealer's own website
                p.phone = p.address = None
            n += 1
    if n:
        audit(db, action="prospect.places_cache_purged", entity_type="system", actor_type="job", details={"count": n})
    return {"purged": n}


HANDLERS = {
    "outreach.agent_tick": handle_tick,
    "outreach.discover": handle_discover,
    "outreach.enrich": handle_enrich,
    "outreach.integration_health": handle_health,
    "outreach.places_purge": handle_places_purge,
}


def ensure_outreach_schedules(db) -> None:
    upsert_schedule(db, "outreach.agent_tick", "outreach.agent_tick", max(30, settings.agent_tick_seconds), queue="outreach")
    upsert_schedule(db, "outreach.integration_health", "outreach.integration_health", 900, queue="outreach")
    upsert_schedule(db, "outreach.places_purge", "outreach.places_purge", 86400, queue="outreach")

"""JSON API (session cookie or ``Authorization: Bearer <api key>``). Shared contract for later phases."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Activity, Prospect, User
from ..services import crm, reports
from ..services.prospects import LeadRecord, intake
from ..services.scoring import score_prospect
from .deps import check_csrf, current_user, require

router = APIRouter(prefix="/api/v1", tags=["api"], dependencies=[Depends(check_csrf)])  # cookie writes need X-CSRF-Token


class ProspectIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    website: str | None = None
    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    phone: str | None = None
    email: str | None = None
    dealership_type: str | None = None
    inventory_url: str | None = None
    source_url: str | None = None
    notes: str = ""


class StageIn(BaseModel):
    stage: str
    reason: str = ""


def prospect_out(p: Prospect) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "stage": p.stage, "dealership_type": p.dealership_type, "website": p.website,
            "address_line": p.address_line, "city": p.city, "state": p.state, "postal_code": p.postal_code,
            "phone": p.phone_e164, "email": p.email, "score": p.score, "lead_source": p.lead_source,
            "discovered_at": p.discovered_at, "last_verified_at": p.last_verified_at, "do_not_contact": p.do_not_contact,
            "simulated": p.simulated, "owner_id": p.owner_id}


@router.get("/prospects")
def list_prospects(stage: str | None = None, city: str | None = None, q: str | None = None, limit: int = 50,
                   offset: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Prospect).where(Prospect.merged_into_id.is_(None))
    if stage:
        stmt = stmt.where(Prospect.stage == stage)
    if city:
        stmt = stmt.where(Prospect.city.ilike(city))
    if q:
        stmt = stmt.where(Prospect.name.ilike(f"%{q}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Prospect.id).offset(max(0, offset)).limit(max(1, min(limit, 200))))
    return {"total": total, "items": [prospect_out(p) for p in rows]}


@router.post("/prospects", status_code=201)
def create_prospect(body: ProspectIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    res = intake(db, LeadRecord(source="api", **body.model_dump()), actor_id=user.id)
    if res.prospect is None:
        raise HTTPException(422, res.errors)
    return {"created": res.created, "matched_by": res.matched_by, "errors": res.errors, "prospect": prospect_out(res.prospect)}


@router.get("/prospects/{pid}")
def get_prospect(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(Prospect, pid)
    if not p:
        raise HTTPException(404)
    timeline = db.scalars(select(Activity).where(Activity.prospect_id == pid).order_by(Activity.created_at.desc()).limit(200))
    return {**prospect_out(p), "score_breakdown": p.score_breakdown,
            "evidence": [{"id": e.id, "field": e.field, "value": e.value, "source_type": e.source_type,
                          "source_url": e.source_url, "collected_at": e.collected_at, "verified": e.verified}
                         for e in p.evidence],
            "contacts": [{"id": c.id, "name": c.name, "role": c.role, "email": c.email, "verified": c.verified,
                          "source_url": c.source_url} for c in p.contacts],
            "timeline": [{"kind": a.kind, "summary": a.summary, "created_at": a.created_at} for a in timeline]}


@router.post("/prospects/{pid}/stage")
def change_stage(pid: int, body: StageIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = db.get(Prospect, pid)
    if not p:
        raise HTTPException(404)
    try:
        crm.set_stage(db, p, body.stage, actor=user, reason=body.reason)
    except crm.StageError as exc:
        raise HTTPException(409, str(exc)) from exc
    return prospect_out(p)


@router.post("/prospects/{pid}/score")
def rescore(pid: int, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = db.get(Prospect, pid)
    if not p:
        raise HTTPException(404)
    return score_prospect(db, p)


@router.get("/reports/daily")
def daily(user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = reports.daily(db)
    for k in ("meetings_needing_prep", "integration_errors", "failed_jobs", "agent_errors"):
        d[k] = len(d[k])
    return d


@router.get("/reports/weekly")
def weekly(include_simulated: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return reports.weekly(db, include_simulated=include_simulated)

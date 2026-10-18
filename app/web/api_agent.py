"""Outreach agent API: dashboard, controls, campaigns, templates, approvals, inbox, suppression."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations import inbox
from app.models.base import utcnow
from app.models.outreach import AgentActivity, Campaign, CompanyFact, InboundMessage, MessageTemplate, OutreachMessage, Prospect, Suppression
from app.security.auth import AuthContext, require_admin, require_staff
from app.services import audit
from app.services.agent import compliance, dashboard, discovery, messaging, runner
from app.services.agent.state import AgentError, authorize_live_mode, get_config, set_campaign_status, set_state
from app.web.api_core import bad

router = APIRouter(prefix="/api/v1/agent", dependencies=[Depends(require_staff)])


@router.get("/dashboard")
def get_dashboard(days: int = 30, db: Session = Depends(get_db)):
    return dashboard.agent_dashboard(db, days)


class StateIn(BaseModel):
    state: str
    reason: str = ""


@router.post("/state")
def change_state(body: StateIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    if body.state == "running" and not ctx.user.is_admin:
        raise HTTPException(403, "Only an admin can start the agent")
    try:
        cfg = set_state(db, body.state, ctx.user, body.reason)
    except AgentError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"state": cfg.state}


@router.post("/emergency-stop")
def emergency_stop(body: StateIn | None = None, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    """Any staff member may trigger an emergency stop."""
    cfg = get_config(db)
    if cfg.state != "emergency_stopped":
        set_state(db, "emergency_stopped", ctx.user, (body.reason if body else "") or "emergency stop")
    db.commit()
    return {"state": "emergency_stopped"}


class LiveIn(BaseModel):
    enabled: bool


@router.post("/live-mode")
def live_mode(body: LiveIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        cfg = authorize_live_mode(db, ctx.user, body.enabled)
    except AgentError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"live_mode": cfg.live_mode}


CONFIG_FIELDS = {"timezone", "send_days", "send_start_hour", "send_end_hour", "daily_send_limit", "hourly_send_limit", "per_domain_daily_limit",
                 "daily_budget_micros", "max_retries", "max_followups", "followup_days", "min_qualified_queue", "research_batch_size",
                 "tick_action_limit", "discovery_queries"}


@router.get("/config")
def get_agent_config(db: Session = Depends(get_db)):
    cfg = get_config(db)
    return {k: getattr(cfg, k) for k in CONFIG_FIELDS | {"state", "live_mode", "enabled_channels"}}


@router.patch("/config")
def patch_config(changes: dict, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    cfg = get_config(db)
    for k, v in changes.items():
        if k not in CONFIG_FIELDS:
            raise HTTPException(400, f"{k} is not configurable here")
        if k.endswith(("limit", "hour", "retries", "followups", "micros", "size", "queue")) and (not isinstance(v, int) or v < 0):
            raise HTTPException(400, f"{k} must be a non-negative integer")
        setattr(cfg, k, v)
    if not (0 <= cfg.send_start_hour < cfg.send_end_hour <= 24):
        raise HTTPException(400, "Invalid send window")
    audit.record(db, "agent.config", actor=ctx.user, details=changes)
    db.commit()
    return get_agent_config(db)


@router.post("/tick")
def run_tick(ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    """Run one bounded tick now (same code path as the scheduled worker job)."""
    return runner.tick(db)


# --------------------------------------------------------------- campaigns --


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    language: str = Field("en", pattern="^(en|es)$")
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=10000)


@router.post("/templates", status_code=201)
def create_template(body: TemplateIn, db: Session = Depends(get_db)):
    try:
        messaging.validate_template(body.subject, body.body)
    except messaging.MessagingError as exc:
        raise bad(exc) from exc
    t = MessageTemplate(**body.model_dump(), approved=False)
    db.add(t)
    db.commit()
    return {"id": t.id, "approved": t.approved}


@router.get("/templates")
def list_templates(db: Session = Depends(get_db)):
    return [{"id": t.id, "name": t.name, "language": t.language, "subject": t.subject, "body": t.body, "approved": t.approved} for t in db.scalars(select(MessageTemplate))]


@router.post("/templates/{tpl_id}/approve")
def approve_template(tpl_id: int, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    t = db.get(MessageTemplate, tpl_id)
    if t is None:
        raise HTTPException(404, "Not found")
    t.approved, t.approved_by_user_id, t.approved_at = True, ctx.user.id, utcnow()
    audit.record(db, "template.approved", actor=ctx.user, entity=t)
    db.commit()
    return {"id": t.id, "approved": True}


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    steps: list[dict] = Field(min_length=1, max_length=6)
    daily_limit: int = Field(20, ge=1, le=500)
    target_cities: list[str] = []


@router.post("/campaigns", status_code=201)
def create_campaign(body: CampaignIn, db: Session = Depends(get_db)):
    for st in body.steps:
        t = db.get(MessageTemplate, st.get("template_id"))
        if t is None:
            raise HTTPException(400, f"Template {st.get('template_id')} not found")
        if not isinstance(st.get("delay_days", 0), int) or st.get("delay_days", 0) < 0:
            raise HTTPException(400, "delay_days must be a non-negative integer")
    c = Campaign(name=body.name, steps=body.steps, daily_limit=body.daily_limit, target_cities=body.target_cities, status="draft", channel="email")
    db.add(c)
    db.commit()
    return {"id": c.id, "status": c.status}


@router.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "status": c.status, "steps": c.steps, "auto_send": c.auto_send_approved_templates, "daily_limit": c.daily_limit} for c in db.scalars(select(Campaign))]


class CampaignStatusIn(BaseModel):
    status: str


@router.post("/campaigns/{campaign_id}/status")
def campaign_status(campaign_id: int, body: CampaignStatusIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Not found")
    if body.status == "active" and not ctx.user.is_admin:
        raise HTTPException(403, "Only an admin can activate a campaign")
    if body.status == "active":
        for st in c.steps:
            t = db.get(MessageTemplate, st["template_id"])
            if not t or not t.approved:
                raise HTTPException(400, "All campaign templates must be approved before activation")
    try:
        set_campaign_status(db, c, body.status, ctx.user)
    except AgentError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"id": c.id, "status": c.status}


@router.post("/campaigns/{campaign_id}/auto-send")
def campaign_auto_send(campaign_id: int, body: LiveIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    """Explicit admin authorization for sending approved-template messages without per-message review."""
    c = db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Not found")
    c.auto_send_approved_templates = body.enabled
    c.auto_send_authorized_by_user_id = ctx.user.id if body.enabled else None
    audit.record(db, "campaign.auto_send", actor=ctx.user, entity=c, details={"enabled": body.enabled})
    db.commit()
    return {"id": c.id, "auto_send": c.auto_send_approved_templates}


class FactIn(BaseModel):
    topic: str = Field(min_length=1, max_length=60)
    keywords: list[str] = Field(min_length=1)
    answer_en: str = Field(min_length=1)
    answer_es: str | None = None
    auto_reply_allowed: bool = False


@router.post("/facts", status_code=201)
def create_fact(body: FactIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    f = CompanyFact(**body.model_dump(), approved=True)
    db.add(f)
    audit.record(db, "company_fact.created", actor=ctx.user, details={"topic": body.topic})
    db.commit()
    return {"id": f.id}


# --------------------------------------------------------------- prospects --


def prospect_out(p: Prospect) -> dict:
    return {k: getattr(p, k) for k in ("id", "business_name", "city", "website", "email", "email_source_url", "phone", "source", "stage", "priority",
                                         "is_independent", "qualification_notes", "disqualify_reason", "evidence", "last_contacted_at", "replied_at")}


@router.get("/prospects")
def list_prospects(stage: str | None = None, limit: int = 200, db: Session = Depends(get_db)):
    stmt = select(Prospect).order_by(Prospect.priority.desc(), Prospect.id)
    if stage:
        stmt = stmt.where(Prospect.stage == stage)
    return [prospect_out(p) for p in db.scalars(stmt.limit(min(limit, 1000)))]


class CsvIn(BaseModel):
    csv: str = Field(max_length=5_000_000)


@router.post("/prospects/import")
def import_prospects(body: CsvIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    res = discovery.import_csv(db, body.csv)
    audit.record(db, "prospects.imported", actor=ctx.user, details={"created": res["created"], "duplicates": res["duplicates"]})
    db.commit()
    return res


class BookIn(BaseModel):
    starts_at: str


@router.post("/prospects/{prospect_id}/book")
def book(prospect_id: int, body: BookIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    from datetime import datetime

    p = db.get(Prospect, prospect_id)
    if p is None:
        raise HTTPException(404, "Not found")
    try:
        start = datetime.fromisoformat(body.starts_at.replace("Z", ""))
    except ValueError as exc:
        raise bad(exc) from exc
    result = messaging.book_meeting(db, p, start, booked_by="human")
    db.commit()
    return {"result": result}


# ---------------------------------------------------------------- messages --


def message_out(m: OutreachMessage) -> dict:
    return {k: getattr(m, k) for k in ("id", "prospect_id", "kind", "to_address", "subject", "body", "status", "generated_by", "requires_approval_reason",
                                         "blocked_reason", "sent_at", "provider", "attempts")}


@router.get("/messages")
def list_messages(status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(OutreachMessage).order_by(OutreachMessage.id.desc())
    if status:
        stmt = stmt.where(OutreachMessage.status == status)
    return [message_out(m) for m in db.scalars(stmt.limit(500))]


class ApproveIn(BaseModel):
    subject: str | None = None
    body: str | None = None


@router.post("/messages/{msg_id}/approve")
def approve(msg_id: int, body: ApproveIn | None = None, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    m = db.get(OutreachMessage, msg_id)
    if m is None:
        raise HTTPException(404, "Not found")
    try:
        messaging.approve_message(db, m, ctx.user, body.subject if body else None, body.body if body else None)
    except messaging.MessagingError as exc:
        raise bad(exc) from exc
    db.commit()
    return message_out(m)


class RejectIn(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/messages/{msg_id}/reject")
def reject(msg_id: int, body: RejectIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    m = db.get(OutreachMessage, msg_id)
    if m is None:
        raise HTTPException(404, "Not found")
    messaging.reject_message(db, m, ctx.user, body.reason)
    db.commit()
    return message_out(m)


# ------------------------------------------------------------------- inbox --


@router.get("/inbound")
def list_inbound(needs_human: bool | None = None, db: Session = Depends(get_db)):
    stmt = select(InboundMessage).order_by(InboundMessage.id.desc())
    if needs_human is not None:
        stmt = stmt.where(InboundMessage.needs_human.is_(needs_human))
    return [{k: getattr(m, k) for k in ("id", "received_at", "from_address", "subject", "body", "prospect_id", "match_method", "classification",
                                         "classification_method", "needs_human", "next_action")} for m in db.scalars(stmt.limit(500))]


class SandboxInboundIn(BaseModel):
    from_address: str
    subject: str | None = None
    body: str
    in_reply_to: str | None = None
    headers: dict = {}


@router.post("/sandbox/inbound", status_code=202)
def sandbox_inbound(body: SandboxInboundIn, db: Session = Depends(get_db)):
    """Queue a simulated inbound email for the sandbox inbox (picked up on the next tick)."""
    if get_config(db).live_mode:
        raise HTTPException(400, "Sandbox inbox is disabled in live mode")
    mid = f"<sandbox-{uuid.uuid4().hex}@sandbox.invalid>"
    inbox.sandbox_inbox.append(inbox.RawInbound(provider_message_id=mid, from_address=body.from_address, subject=body.subject, body=body.body,
                                                in_reply_to=body.in_reply_to, headers=body.headers))
    return {"provider_message_id": mid}


@router.get("/suppressions")
def list_suppressions(db: Session = Depends(get_db)):
    return [{"id": s.id, "kind": s.kind, "value": s.value, "reason": s.reason, "source": s.source, "created_at": s.created_at} for s in db.scalars(select(Suppression).order_by(Suppression.id.desc()))]


class SuppressionIn(BaseModel):
    kind: str = Field(pattern="^(email|domain|phone)$")
    value: str = Field(min_length=3, max_length=254)
    reason: str = "manual_dnc"


@router.post("/suppressions", status_code=201)
def add_suppression(body: SuppressionIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    if body.reason not in ("opt_out", "manual_dnc", "legal", "complaint", "bounce", "existing_client"):
        raise HTTPException(400, "Invalid reason")
    s = compliance.add_suppression(db, body.kind, body.value, body.reason, f"manual:{ctx.user.email}", ctx.user.id)
    if body.kind == "email":
        p = db.scalar(select(Prospect).where(Prospect.email == s.value))
        if p:
            messaging.stop_sequences(db, p.id, "manually suppressed")
    audit.record(db, "suppression.added", actor=ctx.user, details={"kind": body.kind, "reason": body.reason})
    db.commit()
    return {"id": s.id}


@router.get("/activity")
def activity(limit: int = 200, prospect_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(AgentActivity).order_by(AgentActivity.id.desc())
    if prospect_id:
        stmt = stmt.where(AgentActivity.prospect_id == prospect_id)
    return [{"id": a.id, "at": a.created_at, "action": a.action, "outcome": a.outcome, "prospect_id": a.prospect_id, "detail": a.detail,
             "data": a.data, "cost_micros": a.cost_micros, "from_state": a.from_state, "to_state": a.to_state} for a in db.scalars(stmt.limit(min(limit, 2000)))]

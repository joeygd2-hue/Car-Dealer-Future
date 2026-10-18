"""Agency-only outreach agent API (platform staff), plus the public unsubscribe endpoint."""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, platform_admin, platform_user
from app.config import settings
from app.core.audit import audit
from app.core.crypto import encrypt_secret
from app.core.errors import BadRequest, Conflict, NotFound
from app.core.jobs import enqueue
from app.core.time import utcnow
from app.models import (
    Campaign, CompanyFact, Enrollment, Escalation, Meeting, OutreachMessage, PlatformIntegration, Prospect, ProspectContact,
    ProspectObservation, Suppression, User,
)
from app.outreach import agent, discovery, policy
from app.outreach.dashboard import dashboard
from app.outreach.providers import PROVIDERS
from app.outreach.providers.base import Candidate, ProviderError
from app.outreach.providers.sandbox import inject_reply

router = APIRouter(tags=["outreach"])
P = "/api/outreach"


def _u(db, user, action, entity_type, entity_id=None, **details):
    audit(db, action=action, entity_type=entity_type, entity_id=entity_id, actor_type="user", actor_id=user.id, details=details)


@router.get(f"{P}/dashboard")
def get_dashboard(days: int = 30, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    return dashboard(db, days=max(1, min(days, 365)))


# ---- controls
class StopIn(BaseModel):
    reason: str = Field(min_length=3)


@router.post(f"{P}/emergency-stop")
def emergency_stop(body: StopIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    return agent.emergency_stop(db, user.id, body.reason)


@router.post(f"{P}/emergency-stop/clear")
def clear_stop(user: User = Depends(platform_admin), db: Session = Depends(get_db)):
    agent.clear_emergency_stop(db, user.id)
    return {"ok": True}


class PauseIn(BaseModel):
    paused: bool


@router.post(f"{P}/pause")
def pause(body: PauseIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    agent.set_global_pause(db, body.paused, user.id)
    return {"paused": body.paused}


@router.post(f"{P}/tick", status_code=202)
def run_tick_now(user: User = Depends(platform_user), db: Session = Depends(get_db)):
    job, _ = enqueue(db, "outreach.agent_tick", queue="outreach", priority=10,
                     idempotency_key=f"manual-tick:{utcnow().strftime('%Y%m%d%H%M')}")
    _u(db, user, "agent.tick_requested", "job", job.id)
    return {"job_id": job.id}


# ---- integrations
class IntegrationIn(BaseModel):
    kind: str
    provider: str
    name: str
    config: dict[str, Any] = {}
    secret: str | None = None


def _integ_view(i: PlatformIntegration) -> dict:
    return {"id": i.id, "kind": i.kind, "provider": i.provider, "name": i.name, "config": {k: v for k, v in (i.config or {}).items()},
            "has_secret": bool(i.secret_ciphertext), "live": i.provider != "sandbox", "live_authorized": i.live_authorized,
            "authorized_by": i.authorized_by, "health": i.health_status, "health_detail": i.health_detail}


@router.get(f"{P}/integrations")
def list_integrations(user: User = Depends(platform_user), db: Session = Depends(get_db)):
    return [_integ_view(i) for i in db.scalars(select(PlatformIntegration))]


@router.post(f"{P}/integrations", status_code=201)
def create_integration(body: IntegrationIn, user: User = Depends(platform_admin), db: Session = Depends(get_db)):
    if body.provider not in PROVIDERS.get(body.kind, ()):
        raise BadRequest(f"Provider must be one of {PROVIDERS.get(body.kind)} for kind {body.kind}")
    i = PlatformIntegration(kind=body.kind, provider=body.provider, name=body.name,
                            config={k: v for k, v in body.config.items() if "secret" not in k.lower() and "token" not in k.lower()})
    if body.secret:
        i.secret_ciphertext = encrypt_secret(body.secret)
    db.add(i)
    db.flush()
    _u(db, user, "integration.created", "platform_integration", i.id, kind=i.kind, provider=i.provider)
    return _integ_view(i)


class AuthorizeIn(BaseModel):
    live_authorized: bool
    confirmation: str = ""


@router.post(f"{P}/integrations/{{integration_id}}/authorize")
def authorize_integration(integration_id: str, body: AuthorizeIn, user: User = Depends(platform_admin), db: Session = Depends(get_db)):
    i = db.get(PlatformIntegration, integration_id)
    if not i:
        raise NotFound()
    if body.live_authorized and body.confirmation != f"AUTHORIZE {i.name}":
        raise BadRequest(f'Type "AUTHORIZE {i.name}" to confirm live use of this integration')
    i.live_authorized = body.live_authorized
    i.authorized_by, i.authorized_at = (user.id, utcnow()) if body.live_authorized else (None, None)
    _u(db, user, "integration.live_authorization", "platform_integration", i.id, live_authorized=body.live_authorized)
    return _integ_view(i)


# ---- campaigns
class CampaignIn(BaseModel):
    name: str
    sequence: list[dict[str, Any]] = []
    target_criteria: dict[str, Any] = {}
    operating_schedule: dict[str, Any] = {"timezone": "America/Chicago", "days": [0, 1, 2, 3, 4], "start_hour": 9, "end_hour": 17}
    daily_send_limit: int = Field(20, ge=1, le=200)
    hourly_send_limit: int = Field(5, ge=1, le=50)
    daily_budget_usd: float = Field(2.0, ge=0, le=500)
    max_followups: int = Field(2, ge=0, le=4)
    require_approval_first_touch: bool = True
    require_approval_followups: bool = True
    auto_reply_routine: bool = False
    auto_book_meetings: bool = False
    email_integration_id: str | None = None
    calendar_integration_id: str | None = None
    sender_name: str
    sender_email: str
    postal_address: str = Field(min_length=10)
    channel: str = "email"


def _campaign_view(c: Campaign) -> dict:
    return {k: getattr(c, k) for k in ("id", "name", "status", "channel", "sequence", "target_criteria", "operating_schedule",
                                       "daily_send_limit", "hourly_send_limit", "daily_budget_usd", "max_followups",
                                       "require_approval_first_touch", "require_approval_followups", "auto_reply_routine",
                                       "auto_book_meetings", "email_integration_id", "calendar_integration_id", "sender_name",
                                       "sender_email", "postal_address", "live_authorized")}


@router.get(f"{P}/campaigns")
def list_campaigns(user: User = Depends(platform_user), db: Session = Depends(get_db)):
    return [_campaign_view(c) for c in db.scalars(select(Campaign))]


@router.post(f"{P}/campaigns", status_code=201)
def create_campaign(body: CampaignIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    if body.channel not in policy.ALLOWED_CHANNELS:
        raise BadRequest("Only email is enabled. SMS/voice need documented written consent and legal review (see known limitations).")
    c = Campaign(**body.model_dump(), created_by=user.id, status="draft")
    db.add(c)
    db.flush()
    _u(db, user, "campaign.created", "campaign", c.id)
    return _campaign_view(c)


@router.patch(f"{P}/campaigns/{{campaign_id}}")
def update_campaign(campaign_id: str, body: dict[str, Any], user: User = Depends(platform_user), db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise NotFound()
    allowed = set(CampaignIn.model_fields) - {"channel"}
    for k, v in body.items():
        if k in allowed:
            setattr(c, k, v)
    _u(db, user, "campaign.updated", "campaign", c.id, fields=sorted(body))
    return _campaign_view(c)


class StatusIn(BaseModel):
    status: str


@router.post(f"{P}/campaigns/{{campaign_id}}/status")
def campaign_status(campaign_id: str, body: StatusIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise NotFound()
    if body.status not in ("active", "paused", "completed", "draft"):
        raise BadRequest("Invalid status")
    if body.status == "active" and not (c.postal_address and c.sender_email):
        raise BadRequest("A sender email and postal address are required before activating")
    c.status = body.status
    _u(db, user, f"campaign.{body.status}", "campaign", c.id)
    return _campaign_view(c)


@router.post(f"{P}/campaigns/{{campaign_id}}/authorize-live")
def authorize_campaign(campaign_id: str, body: AuthorizeIn, user: User = Depends(platform_admin), db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise NotFound()
    if body.live_authorized and body.confirmation != f"AUTHORIZE {c.name}":
        raise BadRequest(f'Type "AUTHORIZE {c.name}" to confirm live sending for this campaign')
    c.live_authorized = body.live_authorized
    c.live_authorized_by, c.live_authorized_at = (user.id, utcnow()) if body.live_authorized else (None, None)
    _u(db, user, "campaign.live_authorization", "campaign", c.id, live_authorized=body.live_authorized)
    return _campaign_view(c)


# ---- prospects
def _prospect_view(db, p: Prospect, full=False) -> dict:
    out = {k: getattr(p, k) for k in ("id", "business_name", "website", "phone", "city", "state", "postal_code", "source",
                                      "status", "priority_score", "disqualify_reason", "is_simulated", "qualification")}
    if full:
        out["contacts"] = [{"id": c.id, "name": c.name, "email": c.email, "evidence_url": c.evidence_url, "email_status": c.email_status,
                            "suppressed": bool(policy.is_suppressed(db, c.email))}
                           for c in db.scalars(select(ProspectContact).where(ProspectContact.prospect_id == p.id))]
        out["observations"] = [{"id": o.id, "kind": o.kind, "statement": o.statement, "evidence_url": o.evidence_url,
                                "excerpt": o.evidence_excerpt} for o in db.scalars(select(ProspectObservation).where(
                                    ProspectObservation.prospect_id == p.id))]
        out["messages"] = [_msg_view(m) for m in db.scalars(select(OutreachMessage).where(OutreachMessage.prospect_id == p.id)
                                                            .order_by(OutreachMessage.created_at))]
        out["meetings"] = [{"id": m.id, "status": m.status, "start_at": m.start_at.isoformat() if m.start_at else None,
                            "video_link": m.video_link, "offered_slots": m.offered_slots}
                           for m in db.scalars(select(Meeting).where(Meeting.prospect_id == p.id))]
    return out


@router.get(f"{P}/prospects")
def list_prospects(status: str | None = None, limit: int = 200, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    q = select(Prospect)
    if status:
        q = q.where(Prospect.status == status)
    return [_prospect_view(db, p) for p in db.scalars(q.order_by(Prospect.priority_score.desc()).limit(min(limit, 1000)))]


@router.get(f"{P}/prospects/{{prospect_id}}")
def get_prospect(prospect_id: str, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    p = db.get(Prospect, prospect_id)
    if not p:
        raise NotFound()
    return _prospect_view(db, p, full=True)


@router.post(f"{P}/prospects/import", status_code=201)
async def import_prospects(file: UploadFile = File(...), source_attribution: str = Form(..., min_length=5),
                           user: User = Depends(platform_user), db: Session = Depends(get_db)):
    """CSV of dealership prospects from a permitted source (e.g. the TxDMV licensed-dealer lookup).
    Columns: business_name, website, phone, address, city, state, postal_code."""
    text = (await file.read(5_000_001)).decode("utf-8-sig", "replace")
    created = 0
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("business_name") or row.get("name") or "").strip()
        if not name:
            continue
        _, new = discovery.upsert_candidate(db, Candidate(
            business_name=name, source="csv_import", source_ref=None, website=(row.get("website") or "").strip() or None,
            phone=row.get("phone"), address=row.get("address"), city=row.get("city"), state=row.get("state") or "TX",
            postal_code=row.get("postal_code") or row.get("zip"), detail={"attribution": source_attribution}))
        created += int(new)
    _u(db, user, "prospect.imported", "prospect", None, created=created, attribution=source_attribution)
    return {"created": created}


@router.post(f"{P}/prospects/{{prospect_id}}/enrich", status_code=202)
def enrich_prospect(prospect_id: str, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    job, _ = enqueue(db, "outreach.enrich", payload={"prospect_id": prospect_id}, queue="outreach",
                     idempotency_key=f"enrich-manual:{prospect_id}:{utcnow().strftime('%Y%m%d%H')}")
    return {"job_id": job.id}


class DncIn(BaseModel):
    kind: str = "email"
    value: str
    reason: str = "manual_dnc"


@router.get(f"{P}/suppressions")
def list_suppressions(user: User = Depends(platform_user), db: Session = Depends(get_db)):
    return [{"id": s.id, "kind": s.kind, "value": s.value, "reason": s.reason, "source": s.source,
             "expires_at": s.expires_at.isoformat() if s.expires_at else None, "at": s.created_at.isoformat()}
            for s in db.scalars(select(Suppression).order_by(Suppression.created_at.desc()))]


@router.post(f"{P}/suppressions", status_code=201)
def add_suppression(body: DncIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    if body.kind not in ("email", "domain", "phone"):
        raise BadRequest("kind must be email, domain or phone")
    s = policy.suppress(db, body.kind, body.value, body.reason, "manual", by=user.id)
    for c in db.scalars(select(ProspectContact).where(ProspectContact.email == body.value.lower())).all():
        agent.stop_enrollments(db, c.prospect_id, "added to do-not-contact list")
        db.get(Prospect, c.prospect_id).status = "do_not_contact"
    _u(db, user, "suppression.added", "suppression", s.id, kind=body.kind, reason=body.reason)
    return {"id": s.id}


# ---- messages / approvals
def _msg_view(m: OutreachMessage) -> dict:
    return {"id": m.id, "prospect_id": m.prospect_id, "direction": m.direction, "kind": m.kind, "step": m.step, "subject": m.subject,
            "body": m.body, "to": m.to_address, "from": m.from_address, "status": m.status, "approval_required": m.approval_required,
            "classification": m.classification, "confidence": m.classification_confidence, "policy_checks": m.policy_checks,
            "evidence_refs": m.evidence_refs, "generator": m.generator, "error": m.error,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None, "received_at": m.received_at.isoformat() if m.received_at else None,
            "created_at": m.created_at.isoformat()}


@router.get(f"{P}/messages")
def list_messages(status: str | None = None, direction: str | None = None, limit: int = 200, user: User = Depends(platform_user),
                  db: Session = Depends(get_db)):
    q = select(OutreachMessage)
    if status:
        q = q.where(OutreachMessage.status == status)
    if direction:
        q = q.where(OutreachMessage.direction == direction)
    return [_msg_view(m) for m in db.scalars(q.order_by(OutreachMessage.created_at.desc()).limit(min(limit, 1000)))]


class ApproveMsgIn(BaseModel):
    subject: str | None = None
    body: str | None = None


@router.post(f"{P}/messages/{{message_id}}/approve")
def approve_msg(message_id: str, body: ApproveMsgIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    edits = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return _msg_view(agent.approve_message(db, message_id, user.id, edits or None))
    except ValueError as exc:
        raise Conflict(str(exc)) from exc


class RejectIn(BaseModel):
    reason: str = Field(min_length=3)


@router.post(f"{P}/messages/{{message_id}}/reject")
def reject_msg(message_id: str, body: RejectIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    if not db.get(OutreachMessage, message_id):
        raise NotFound()
    return _msg_view(agent.reject_message(db, message_id, user.id, body.reason))


# ---- escalations & meetings
@router.get(f"{P}/escalations")
def list_escalations(status: str = "open", user: User = Depends(platform_user), db: Session = Depends(get_db)):
    return [{"id": e.id, "kind": e.kind, "summary": e.summary, "prospect_id": e.prospect_id, "message_id": e.message_id,
             "status": e.status, "at": e.created_at.isoformat(), "resolution": e.resolution}
            for e in db.scalars(select(Escalation).where(Escalation.status == status).order_by(Escalation.created_at.desc()))]


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=3)


@router.post(f"{P}/escalations/{{esc_id}}/resolve")
def resolve(esc_id: str, body: ResolveIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    e = db.get(Escalation, esc_id)
    if not e:
        raise NotFound()
    e.status, e.resolution, e.resolved_by, e.resolved_at = "resolved", body.resolution, user.id, utcnow()
    _u(db, user, "escalation.resolved", "escalation", e.id)
    return {"ok": True}


class BookIn(BaseModel):
    slot_index: int = Field(ge=0, le=9)


@router.post(f"{P}/meetings/{{meeting_id}}/book")
def book(meeting_id: str, body: BookIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    m = db.get(Meeting, meeting_id)
    if not m or m.status != "offered":
        raise Conflict("Meeting is not awaiting booking")
    if body.slot_index >= len(m.offered_slots):
        raise BadRequest("No such slot")
    try:
        agent.book_meeting(db, m, body.slot_index, booked_by=user.id)
    except ProviderError as exc:
        raise Conflict(str(exc)) from exc
    return {"id": m.id, "status": m.status, "start_at": m.start_at.isoformat(), "video_link": m.video_link}


# ---- company facts (approved answers)
class FactIn(BaseModel):
    topic: str
    keywords: list[str] = []
    answer: str


@router.get(f"{P}/company-facts")
def list_facts(user: User = Depends(platform_user), db: Session = Depends(get_db)):
    return [{"id": f.id, "topic": f.topic, "keywords": f.keywords, "answer": f.answer, "active": f.active,
             "approved_by": f.approved_by} for f in db.scalars(select(CompanyFact))]


@router.post(f"{P}/company-facts", status_code=201)
def upsert_fact(body: FactIn, user: User = Depends(platform_admin), db: Session = Depends(get_db)):
    f = db.scalar(select(CompanyFact).where(CompanyFact.topic == body.topic))
    if not f:
        f = CompanyFact(topic=body.topic)
        db.add(f)
    f.keywords, f.answer, f.approved_by, f.approved_at, f.active = body.keywords, body.answer, user.id, utcnow(), True
    db.flush()
    _u(db, user, "company_fact.approved", "company_fact", f.id, topic=body.topic)
    return {"id": f.id}


# ---- sandbox helpers (never available for live integrations)
class InjectIn(BaseModel):
    message_id: str
    body: str
    from_address: str | None = None
    subject: str | None = None


@router.post(f"{P}/sandbox/inject-reply", status_code=201)
def sandbox_inject(body: InjectIn, user: User = Depends(platform_user), db: Session = Depends(get_db)):
    m = db.get(OutreachMessage, body.message_id)
    if not m or m.direction != "outbound" or m.status != "sent":
        raise BadRequest("Pick a sent outbound message")
    integ = db.get(PlatformIntegration, m.integration_id) if m.integration_id else None
    if not integ or integ.provider != "sandbox":
        raise BadRequest("Simulated replies can only be injected into sandbox mailboxes")
    r = inject_reply(db, thread_id=m.thread_id, from_address=body.from_address or m.to_address, to_address=m.from_address or "",
                     subject=body.subject or f"Re: {m.subject}", body=body.body, in_reply_to=m.provider_message_id)
    _u(db, user, "sandbox.reply_injected", "sandbox_mail", r.id)
    return {"id": r.id}


# ---- public one-click unsubscribe (no login; RFC 8058 POST supported)
@router.get("/u/{token}", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe_page(token: str, db: Session = Depends(get_db)):
    if not policy.verify_unsubscribe_token(token):
        return HTMLResponse("<p>This unsubscribe link is invalid.</p>", status_code=400)
    return HTMLResponse(f"""<!doctype html><meta name=viewport content="width=device-width"><title>Unsubscribe</title>
<body style="font-family:sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem">
<h1>Unsubscribe</h1><p>Click below to stop receiving our marketing emails.</p>
<form method="post" action="/u/{token}"><button type="submit">Unsubscribe me</button></form></body>""")


@router.post("/u/{token}", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe(token: str, db: Session = Depends(get_db)):
    mid = policy.verify_unsubscribe_token(token)
    if not mid:
        return HTMLResponse("<p>This unsubscribe link is invalid.</p>", status_code=400)
    m = db.get(OutreachMessage, mid)
    if m and m.to_address:
        policy.suppress(db, "email", m.to_address, "opt_out", f"unsubscribe_link:{mid}")
        if m.prospect_id:
            agent.stop_enrollments(db, m.prospect_id, "unsubscribed via link")
            db.get(Prospect, m.prospect_id).status = "do_not_contact"
        audit(db, action="outreach.unsubscribed", entity_type="outreach_message", entity_id=mid, actor_type="system",
              details={"method": "link"})
    return HTMLResponse("<p>You have been unsubscribed. You will not receive further marketing emails from us.</p>")

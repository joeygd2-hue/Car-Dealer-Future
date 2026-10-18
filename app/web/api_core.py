"""Staff API: clients, onboarding, integrations, tickets, dealer operations, audit log."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.base import utcnow
from app.models.core import AuditLog, Client, IntegrationConnection, SupportTicket, User
from app.models.dealer_ops import Conversation, ConversationMessage, Listing, PublishAttempt, Vehicle
from app.security.auth import AuthContext, require_admin, require_staff
from app.security.crypto import hash_password
from app.services import audit, dealer_ops, onboarding, tickets

router = APIRouter(prefix="/api/v1")


def bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def client_or_404(db: Session, client_id: int) -> Client:
    c = db.get(Client, client_id)
    if c is None:
        raise HTTPException(404, "Not found")
    return c


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = Field(None, max_length=200)
    address_line1: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=50)
    postal_code: str | None = Field(None, max_length=20)
    phone: str | None = Field(None, max_length=40)
    website: str | None = Field(None, max_length=300)
    contact_name: str | None = Field(None, max_length=200)
    contact_email: EmailStr | None = None
    timezone: str = "America/Chicago"
    source_prospect_id: int | None = None


def client_out(c: Client) -> dict:
    return {k: getattr(c, k) for k in ("id", "name", "legal_name", "status", "city", "state", "phone", "website", "contact_name", "contact_email", "timezone")}


@router.get("/me")
def me(ctx: AuthContext = Depends(require_staff)):
    return {"id": ctx.user.id, "email": ctx.user.email, "role": ctx.user.role}


@router.get("/clients")
def list_clients(ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    return [client_out(c) for c in db.scalars(select(Client).order_by(Client.name))]


@router.post("/clients", status_code=201)
def create_client(body: ClientIn, request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    c = Client(**body.model_dump())
    db.add(c)
    db.flush()
    onboarding.ensure_checklist(db, c)
    if body.source_prospect_id:
        from app.models.outreach import Prospect
        from app.services.agent.compliance import add_suppression
        from app.services.agent.messaging import stop_sequences

        p = db.get(Prospect, body.source_prospect_id)
        if p:
            p.stage = "converted"
            stop_sequences(db, p.id, "converted to client")
            if p.email:
                add_suppression(db, "email", p.email, "existing_client", f"client:{c.id}")
    audit.record(db, "client.created", actor=ctx.user, entity=c, ip=request.client.host if request.client else None)
    db.commit()
    return client_out(c)


@router.get("/clients/{client_id}")
def get_client(client_id: int, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    return client_out(client_or_404(db, client_id))


class ClientUserIn(BaseModel):
    email: EmailStr
    full_name: str | None = None
    password: str = Field(min_length=12, max_length=200)


@router.post("/clients/{client_id}/users", status_code=201)
def create_client_user(client_id: int, body: ClientUserIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    client_or_404(db, client_id)
    if db.scalar(select(User.id).where(User.email == body.email.lower())):
        raise HTTPException(409, "Email already in use")
    u = User(email=body.email.lower(), full_name=body.full_name, password_hash=hash_password(body.password), role="client", client_id=client_id)
    db.add(u)
    db.flush()
    audit.record(db, "user.created", actor=ctx.user, client_id=client_id, entity=u, details={"role": "client"})
    db.commit()
    return {"id": u.id, "email": u.email, "role": u.role, "client_id": client_id}


@router.get("/clients/{client_id}/onboarding")
def get_onboarding(client_id: int, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    c = client_or_404(db, client_id)
    steps = onboarding.ensure_checklist(db, c)
    db.commit()
    return {
        "steps": [{"code": s.code, "label": onboarding.STEP_LABELS[s.code], "status": s.status, "notes": s.notes, "evidence": s.evidence} for s in steps],
        "automations": {f: {"label": spec["label"], "enabled": onboarding.automation_enabled(db, client_id, f), "outstanding": onboarding.outstanding_prereqs(db, client_id, f)}
                        for f, spec in onboarding.AUTOMATION_PREREQS.items()},
    }


class StepIn(BaseModel):
    status: str
    notes: str | None = None
    evidence: str | None = None


@router.post("/clients/{client_id}/onboarding/{code}")
def set_onboarding_step(client_id: int, code: str, body: StepIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    client_or_404(db, client_id)
    try:
        s = onboarding.set_step(db, client_id, code, body.status, ctx.user, body.notes, body.evidence)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"code": s.code, "status": s.status}


class ToggleIn(BaseModel):
    enabled: bool


@router.post("/clients/{client_id}/automation/{feature}")
def set_automation(client_id: int, feature: str, body: ToggleIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    client_or_404(db, client_id)
    try:
        row = onboarding.set_automation(db, client_id, feature, body.enabled, ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"feature": row.feature, "enabled": row.enabled}


class IntegrationIn(BaseModel):
    kind: str = Field(max_length=40)
    provider: str = Field(max_length=60)
    status: str
    authorization_note: str | None = None


@router.post("/clients/{client_id}/integrations")
def record_integration(client_id: int, body: IntegrationIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    """Record an integration's status. `connected` requires a written note of how it was verified."""
    client_or_404(db, client_id)
    if body.status not in ("not_connected", "sandbox", "connected", "error", "disabled"):
        raise HTTPException(400, "Invalid status")
    if body.status == "connected" and not (body.authorization_note or "").strip():
        raise HTTPException(400, "Describe how the connection was authorized and verified")
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == client_id, IntegrationConnection.kind == body.kind, IntegrationConnection.provider == body.provider))
    if conn is None:
        conn = IntegrationConnection(client_id=client_id, kind=body.kind, provider=body.provider, settings={})
        db.add(conn)
    conn.status, conn.authorization_note, conn.authorized_by_user_id = body.status, body.authorization_note, ctx.user.id
    conn.verified_at = utcnow() if body.status == "connected" else None
    db.flush()
    audit.record(db, "integration.status", actor=ctx.user, entity=conn, details={"kind": body.kind, "provider": body.provider, "status": body.status})
    db.commit()
    return {"id": conn.id, "kind": conn.kind, "status": conn.status}


class TicketIn(BaseModel):
    client_id: int
    kind: str
    title: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=10000)
    priority: str = "normal"


def ticket_out(t: SupportTicket) -> dict:
    return {k: getattr(t, k) for k in ("id", "client_id", "kind", "title", "description", "priority", "status", "source", "created_at", "resolved_at")}


@router.get("/tickets")
def list_tickets(status: str | None = None, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    stmt = select(SupportTicket).order_by(SupportTicket.created_at.desc())
    if status:
        stmt = stmt.where(SupportTicket.status == status)
    return [ticket_out(t) for t in db.scalars(stmt.limit(500))]


@router.post("/tickets", status_code=201)
def create_ticket(body: TicketIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    client_or_404(db, body.client_id)
    try:
        t = tickets.create_ticket(db, client_id=body.client_id, kind=body.kind, title=body.title, description=body.description, priority=body.priority, actor=ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return ticket_out(t)


class TicketStatusIn(BaseModel):
    status: str
    resolution: str | None = None


@router.patch("/tickets/{ticket_id}")
def update_ticket(ticket_id: int, body: TicketStatusIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    t = db.get(SupportTicket, ticket_id)
    if t is None:
        raise HTTPException(404, "Not found")
    try:
        tickets.update_status(db, t, body.status, ctx.user, body.resolution)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return ticket_out(t)


# ---- dealer operations (Phase 2/3 shared scaffolding) ----------------------


class CsvIn(BaseModel):
    csv: str = Field(max_length=5_000_000)


@router.post("/clients/{client_id}/inventory/import")
def import_inventory(client_id: int, body: CsvIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    c = client_or_404(db, client_id)
    imp = dealer_ops.import_inventory_csv(db, c, body.csv, ctx.user)
    db.commit()
    return {"id": imp.id, "created": imp.rows_created, "updated": imp.rows_updated, "failed": imp.rows_failed, "errors": imp.errors[:20]}


@router.post("/clients/{client_id}/listings/generate")
def generate_listings(client_id: int, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    client_or_404(db, client_id)
    out = []
    for v in db.scalars(select(Vehicle).where(Vehicle.client_id == client_id, Vehicle.status == "available")):
        out += [{"id": li.id, "vehicle_id": v.id, "language": li.language, "status": li.status} for li in dealer_ops.generate_listings(db, v)]
    db.commit()
    return out


def _listing(db: Session, client_id: int, listing_id: int) -> Listing:
    li = db.scalar(select(Listing).where(Listing.id == listing_id, Listing.client_id == client_id))
    if li is None:
        raise HTTPException(404, "Not found")
    return li


@router.post("/clients/{client_id}/listings/{listing_id}/approve")
def approve_listing(client_id: int, listing_id: int, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    li = _listing(db, client_id, listing_id)
    dealer_ops.approve_listing(db, li, ctx.user)
    db.commit()
    return {"id": li.id, "status": li.status}


class PublishIn(BaseModel):
    channel: str = Field(max_length=40)


@router.post("/clients/{client_id}/listings/{listing_id}/publish")
def publish_listing(client_id: int, listing_id: int, body: PublishIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    li = _listing(db, client_id, listing_id)
    try:
        a = dealer_ops.publish_listing(db, li, body.channel)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"id": a.id, "status": a.status, "note": a.note}


class ConfirmPublishIn(BaseModel):
    url: str = Field(max_length=500)


@router.post("/clients/{client_id}/publish-attempts/{attempt_id}/confirm")
def confirm_publish(client_id: int, attempt_id: int, body: ConfirmPublishIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    a = db.scalar(select(PublishAttempt).where(PublishAttempt.id == attempt_id, PublishAttempt.client_id == client_id))
    if a is None:
        raise HTTPException(404, "Not found")
    dealer_ops.confirm_manual_publication(db, a, body.url, ctx.user)
    db.commit()
    return {"id": a.id, "status": a.status}


class InquiryIn(BaseModel):
    channel: str = "web_form"
    customer_name: str | None = None
    customer_contact: str | None = None
    body: str = Field(min_length=1, max_length=10000)
    language: str = "en"
    vehicle_id: int | None = None
    provider_message_id: str = Field(min_length=1, max_length=300)


@router.post("/clients/{client_id}/inquiries/sandbox", status_code=201)
def sandbox_inquiry(client_id: int, body: InquiryIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    """Simulate an inbound customer inquiry on a connected test channel."""
    c = client_or_404(db, client_id)
    v = db.scalar(select(Vehicle).where(Vehicle.id == body.vehicle_id, Vehicle.client_id == client_id)) if body.vehicle_id else None
    conv = dealer_ops.receive_inquiry(db, c, channel=body.channel, customer_name=body.customer_name, customer_contact=body.customer_contact,
                                      body=body.body, language=body.language, vehicle=v, provider_message_id=body.provider_message_id)
    db.commit()
    return {"conversation_id": conv.id, "status": conv.status, "language": conv.language}


class DraftIn(BaseModel):
    template: str


@router.post("/clients/{client_id}/conversations/{conv_id}/draft")
def draft_response(client_id: int, conv_id: int, body: DraftIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    conv = db.scalar(select(Conversation).where(Conversation.id == conv_id, Conversation.client_id == client_id))
    if conv is None:
        raise HTTPException(404, "Not found")
    if body.template not in dealer_ops.APPROVED_RESPONSES:
        raise HTTPException(400, "Unknown approved response")
    m = dealer_ops.draft_response(db, conv, body.template)
    db.commit()
    return {"message_id": m.id, "body": m.body, "language": m.language, "status": m.status}


@router.post("/clients/{client_id}/conversations/{conv_id}/messages/{msg_id}/approve")
def approve_response(client_id: int, conv_id: int, msg_id: int, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    m = db.scalar(select(ConversationMessage).where(ConversationMessage.id == msg_id, ConversationMessage.conversation_id == conv_id, ConversationMessage.client_id == client_id))
    if m is None:
        raise HTTPException(404, "Not found")
    try:
        dealer_ops.approve_and_send_response(db, m, ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    conv = db.get(Conversation, conv_id)
    summary = dealer_ops.summarize_and_deliver(db, conv)
    db.commit()
    return {"status": m.status, "summary": summary, "summary_delivered": conv.summary_delivered_at is not None}


@router.get("/audit-log")
def audit_log(limit: int = 200, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000)))
    return [{"id": r.id, "at": r.created_at, "actor": r.actor_label, "action": r.action, "entity": f"{r.entity_type}:{r.entity_id}", "client_id": r.client_id, "details": r.details} for r in rows]


def _today() -> date:
    return utcnow().date()

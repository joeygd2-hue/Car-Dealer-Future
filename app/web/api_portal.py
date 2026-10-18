"""Client-portal API. The client is ALWAYS taken from the authenticated user, never from the request."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models.core import SupportTicket
from app.models.dealer_ops import Conversation
from app.models.finance import Invoice
from app.models.seo import ClientReport, SeoIssue
from app.security.auth import AuthContext, get_owned_or_404, require_client_user, scope_to_user
from app.services import finance, tickets

router = APIRouter(prefix="/api/v1/portal")


@router.get("/me")
def me(ctx: AuthContext = Depends(require_client_user)):
    c = ctx.user.client
    return {"user": ctx.user.email, "client": {"id": c.id, "name": c.name, "status": c.status}}


@router.get("/reports")
def reports(ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    stmt = scope_to_user(select(ClientReport), ClientReport, ctx.user).where(ClientReport.status.in_(("final", "sent"))).order_by(ClientReport.period_start.desc())
    return [{"id": r.id, "period_start": r.period_start, "period_end": r.period_end, "status": r.status} for r in db.scalars(stmt)]


@router.get("/reports/{report_id}")
def report(report_id: int, ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    r = get_owned_or_404(db, ClientReport, report_id, ctx.user)
    if r.status == "draft":
        raise HTTPException(404, "Not found")
    return {"id": r.id, "data": r.data}


@router.get("/invoices")
def invoices(ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    stmt = scope_to_user(select(Invoice), Invoice, ctx.user).options(selectinload(Invoice.lines), selectinload(Invoice.payments), selectinload(Invoice.credits)).where(Invoice.status != "draft")
    return [{"id": i.id, "number": i.number, "issue_date": i.issue_date, "due_date": i.due_date, "status": finance.invoice_display_status(i),
             "total_cents": finance.invoice_total(i), "balance_cents": finance.invoice_balance(i)} for i in db.scalars(stmt)]


@router.get("/invoices/{invoice_id}")
def invoice(invoice_id: int, ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    i = get_owned_or_404(db, Invoice, invoice_id, ctx.user)
    if i.status == "draft":
        raise HTTPException(404, "Not found")
    return {"id": i.id, "number": i.number, "status": finance.invoice_display_status(i), "total_cents": finance.invoice_total(i),
            "balance_cents": finance.invoice_balance(i), "lines": [{"description": li.description, "amount_cents": li.amount_cents} for li in i.lines]}


@router.get("/seo-issues")
def seo_issues(ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    stmt = scope_to_user(select(SeoIssue), SeoIssue, ctx.user).order_by(SeoIssue.status, SeoIssue.severity)
    return [{"id": i.id, "finding": i.finding, "url": i.url, "severity": i.severity, "status": i.status, "detection": i.detection,
             "recommended_fix": i.recommended_fix, "responsible_party": i.responsible_party} for i in db.scalars(stmt)]


@router.get("/conversations")
def conversations(ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    stmt = scope_to_user(select(Conversation), Conversation, ctx.user).order_by(Conversation.id.desc()).limit(200)
    return [{"id": c.id, "channel": c.channel, "status": c.status, "language": c.language, "summary": c.summary, "created_at": c.created_at} for c in db.scalars(stmt)]


@router.get("/tickets")
def list_tickets(ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    stmt = scope_to_user(select(SupportTicket), SupportTicket, ctx.user).order_by(SupportTicket.id.desc())
    return [{"id": t.id, "kind": t.kind, "title": t.title, "status": t.status, "created_at": t.created_at} for t in db.scalars(stmt)]


class PortalTicketIn(BaseModel):
    kind: str = Field(pattern="^(client_issue|change_request|other)$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=10000)


@router.post("/tickets", status_code=201)
def create_ticket(body: PortalTicketIn, ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    t = tickets.create_ticket(db, client_id=ctx.user.client_id, kind=body.kind, title=body.title, description=body.description, actor=ctx.user)
    db.commit()
    return {"id": t.id, "status": t.status}

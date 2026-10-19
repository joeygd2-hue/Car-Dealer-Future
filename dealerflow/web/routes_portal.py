"""Client portal (/portal) and its JSON API (/api/v1/portal). The client is always the logged-in user's client."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import Client, ClientReport, Conversation, Invoice, SeoIssue, SupportTicket, User
from ..services import finance, tickets
from ..services.tenancy import owned_or_404, scope
from .deps import check_csrf, flash, render, require_client

router = APIRouter(dependencies=[Depends(check_csrf)])
PUBLISHED = ("final", "sent")


def _published_reports(db: Session, u: User):
    return db.scalars(scope(select(ClientReport), ClientReport, u).where(ClientReport.status.in_(PUBLISHED))
                      .order_by(ClientReport.period_start.desc())).all()


def _issued_invoices(db: Session, u: User):
    return db.scalars(scope(select(Invoice), Invoice, u).options(selectinload(Invoice.lines), selectinload(Invoice.payments),
                                                                 selectinload(Invoice.credits))
                      .where(Invoice.status != "draft").order_by(Invoice.issue_date.desc())).all()


def _published_report(db: Session, rid: int, u: User) -> ClientReport:
    r = owned_or_404(db, ClientReport, rid, u)
    if r.status not in PUBLISHED:
        raise HTTPException(404, "Not found")
    return r


@router.get("/portal")
def portal_home(request: Request, user: User = Depends(require_client), db: Session = Depends(get_db)):
    return render(request, "portal.html", user, client=db.get(Client, user.client_id), reports=_published_reports(db, user),
                  invoices=_issued_invoices(db, user), f=finance,
                  issues=db.scalars(scope(select(SeoIssue), SeoIssue, user).order_by(SeoIssue.status, SeoIssue.severity)).all(),
                  conversations=db.scalars(scope(select(Conversation), Conversation, user).where(Conversation.simulated.is_(False))
                                           .order_by(Conversation.id.desc()).limit(25)).all(),
                  tickets=db.scalars(scope(select(SupportTicket), SupportTicket, user).order_by(SupportTicket.id.desc())).all())


@router.get("/portal/reports/{rid}")
def portal_report(rid: int, request: Request, user: User = Depends(require_client), db: Session = Depends(get_db)):
    r = _published_report(db, rid, user)
    return render(request, "client_report.html", user, report=r, d=r.data, staff=False, client=db.get(Client, user.client_id))


@router.post("/portal/tickets")
def portal_ticket(request: Request, kind: str = Form(...), title: str = Form(...), description: str = Form(""),
                  user: User = Depends(require_client), db: Session = Depends(get_db)):
    if kind not in ("client_issue", "change_request", "other"):
        flash(request, "Invalid request type.", "error")
    else:
        try:
            tickets.create_ticket(db, client_id=user.client_id, kind=kind, title=title, description=description, actor=user)
            flash(request, "Request submitted. Our team will follow up.")
        except ValueError as exc:
            flash(request, str(exc), "error")
    return RedirectResponse("/portal#support", status_code=303)


# --- JSON API ------------------------------------------------------------------------------


@router.get("/api/v1/portal/me")
def api_me(user: User = Depends(require_client), db: Session = Depends(get_db)):
    c = db.get(Client, user.client_id)
    return {"user": user.email, "client": {"id": c.id, "name": c.name, "status": c.status}}


@router.get("/api/v1/portal/reports")
def api_reports(user: User = Depends(require_client), db: Session = Depends(get_db)):
    return [{"id": r.id, "period_start": r.period_start, "period_end": r.period_end, "status": r.status} for r in _published_reports(db, user)]


@router.get("/api/v1/portal/reports/{rid}")
def api_report(rid: int, user: User = Depends(require_client), db: Session = Depends(get_db)):
    return {"id": rid, "data": _published_report(db, rid, user).data}


@router.get("/api/v1/portal/invoices")
def api_invoices(user: User = Depends(require_client), db: Session = Depends(get_db)):
    return [{"id": i.id, "number": i.number, "issue_date": i.issue_date, "due_date": i.due_date, "status": finance.invoice_display_status(i),
             "total_cents": finance.invoice_total(i), "balance_cents": finance.invoice_balance(i)} for i in _issued_invoices(db, user)]


@router.get("/api/v1/portal/invoices/{iid}")
def api_invoice(iid: int, user: User = Depends(require_client), db: Session = Depends(get_db)):
    i = owned_or_404(db, Invoice, iid, user)
    if i.status == "draft":
        raise HTTPException(404, "Not found")
    return {"id": i.id, "number": i.number, "status": finance.invoice_display_status(i), "total_cents": finance.invoice_total(i),
            "balance_cents": finance.invoice_balance(i), "lines": [{"description": li.description, "amount_cents": li.amount_cents} for li in i.lines]}


@router.get("/api/v1/portal/seo-issues")
def api_issues(user: User = Depends(require_client), db: Session = Depends(get_db)):
    return [{"id": i.id, "finding": i.finding, "url": i.url, "severity": i.severity, "status": i.status, "detection": i.detection,
             "recommended_fix": i.recommended_fix} for i in db.scalars(scope(select(SeoIssue), SeoIssue, user))]


@router.get("/api/v1/portal/conversations")
def api_conversations(user: User = Depends(require_client), db: Session = Depends(get_db)):
    q = scope(select(Conversation), Conversation, user).where(Conversation.simulated.is_(False)).order_by(Conversation.id.desc()).limit(200)
    return [{"id": c.id, "channel": c.channel, "status": c.status, "language": c.language, "summary": c.summary} for c in db.scalars(q)]


@router.get("/api/v1/portal/tickets")
def api_tickets(user: User = Depends(require_client), db: Session = Depends(get_db)):
    return [{"id": t.id, "kind": t.kind, "title": t.title, "status": t.status}
            for t in db.scalars(scope(select(SupportTicket), SupportTicket, user).order_by(SupportTicket.id.desc()))]


class TicketIn(BaseModel):
    kind: str = Field(pattern="^(client_issue|change_request|other)$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=10000)


@router.post("/api/v1/portal/tickets", status_code=201)
def api_ticket(body: TicketIn, user: User = Depends(require_client), db: Session = Depends(get_db)):
    t = tickets.create_ticket(db, client_id=user.client_id, kind=body.kind, title=body.title, description=body.description, actor=user)
    return {"id": t.id, "status": t.status}

"""Client portal UI (/portal). Every query is scoped to the logged-in client user's client."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models.core import SupportTicket
from app.models.dealer_ops import Conversation
from app.models.finance import Invoice
from app.models.seo import ClientReport, SeoIssue
from app.security.auth import AuthContext, get_owned_or_404, require_client_user, scope_to_user
from app.services import finance, tickets
from app.web.common import redirect, render

router = APIRouter(prefix="/portal")


@router.get("", response_class=HTMLResponse)
def home(request: Request, ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    u = ctx.user
    reports = db.scalars(scope_to_user(select(ClientReport), ClientReport, u).where(ClientReport.status.in_(("final", "sent"))).order_by(ClientReport.period_start.desc())).all()
    invoices = db.scalars(scope_to_user(select(Invoice), Invoice, u).options(selectinload(Invoice.lines), selectinload(Invoice.payments), selectinload(Invoice.credits))
                          .where(Invoice.status != "draft").order_by(Invoice.issue_date.desc())).all()
    issues = db.scalars(scope_to_user(select(SeoIssue), SeoIssue, u).order_by(SeoIssue.status, SeoIssue.severity)).all()
    convs = db.scalars(scope_to_user(select(Conversation), Conversation, u).order_by(Conversation.id.desc()).limit(25)).all()
    tix = db.scalars(scope_to_user(select(SupportTicket), SupportTicket, u).order_by(SupportTicket.id.desc())).all()
    return render(request, "portal/home.html", ctx, client=u.client, reports=reports, invoices=invoices, issues=issues, conversations=convs, tickets=tix, f=finance)


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report(report_id: int, request: Request, ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    r = get_owned_or_404(db, ClientReport, report_id, ctx.user)
    if r.status == "draft":
        raise HTTPException(404, "Not found")
    return render(request, "report.html", ctx, report=r, d=r.data, staff=False, client=ctx.user.client)


@router.post("/tickets")
def ticket(kind: str = Form(...), title: str = Form(...), description: str = Form(""), ctx: AuthContext = Depends(require_client_user), db: Session = Depends(get_db)):
    if kind not in ("client_issue", "change_request", "other"):
        return redirect("/portal#support", err="Invalid request type")
    try:
        tickets.create_ticket(db, client_id=ctx.user.client_id, kind=kind, title=title, description=description, actor=ctx.user)
        db.commit()
    except ValueError as exc:
        return redirect("/portal#support", err=str(exc))
    return redirect("/portal#support", msg="Request submitted")

"""Client dealerships: workspace, onboarding checklist, automations, integrations, agreements, users, tickets."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db, utcnow
from ..models import Client, ClientAgreement, IntegrationConnection, Prospect, ServicePackage, SupportTicket, User
from ..security import hash_password
from ..services import finance, onboarding, tenancy, tickets
from ..services.client_reports import CONNECTABLE_CHANNELS
from .deps import check_csrf, flash, render, require
from .forms import parse_date, parse_money

router = APIRouter(dependencies=[Depends(check_csrf)])


def _client(db: Session, cid: int) -> Client:
    c = db.get(Client, cid)
    if c is None:
        raise HTTPException(404, "Client not found")
    return c


def _back(cid: int, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/clients/{cid}{anchor}", status_code=303)


@router.get("/clients")
def clients_page(request: Request, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    won = db.scalars(select(Prospect).where(Prospect.stage == "won", ~Prospect.id.in_(
        select(Client.prospect_id).where(Client.prospect_id.is_not(None))))).all()
    return render(request, "clients.html", user, clients=db.scalars(select(Client).order_by(Client.name)).all(), won=won)


@router.post("/clients")
def client_create(request: Request, name: str = Form(""), prospect_id: str = Form(""), city: str = Form(""), phone: str = Form(""),
                  website: str = Form(""), contact_email: str = Form(""), user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    prospect = db.get(Prospect, int(prospect_id)) if prospect_id else None
    if prospect is not None and prospect.stage != "won":
        flash(request, "Only a won prospect can become a client.", "error")
        return RedirectResponse("/clients", status_code=303)
    name = name.strip() or (prospect.name if prospect else "")
    if not name:
        flash(request, "Name is required.", "error")
        return RedirectResponse("/clients", status_code=303)
    extra = {k: v.strip() for k, v in {"city": city, "phone": phone, "website": website, "contact_email": contact_email}.items() if v.strip()}
    c = tenancy.create_client(db, name=name, actor=user, prospect=prospect, **extra)
    flash(request, "Client workspace created. Work through the onboarding checklist before activating services.")
    return _back(c.id)


@router.get("/clients/{cid}")
def client_detail(cid: int, request: Request, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    c = _client(db, cid)
    steps = onboarding.ensure_checklist(db, c)
    autos = {f: {"label": spec["label"], "enabled": onboarding.automation_enabled(db, c.id, f),
                 "outstanding": onboarding.outstanding_prereqs(db, c.id, f)} for f, spec in onboarding.AUTOMATION_PREREQS.items()}
    return render(request, "client_detail.html", user, client=c, steps=steps, labels=onboarding.STEP_LABELS, automations=autos,
                  integrations=db.scalars(select(IntegrationConnection).where(IntegrationConnection.client_id == c.id)).all(),
                  channel_kinds=CONNECTABLE_CHANNELS,
                  agreements=db.scalars(select(ClientAgreement).where(ClientAgreement.client_id == c.id).order_by(ClientAgreement.id)).all(),
                  tickets=db.scalars(select(SupportTicket).where(SupportTicket.client_id == c.id).order_by(SupportTicket.id.desc())).all(),
                  client_users=db.scalars(select(User).where(User.client_id == c.id)).all(),
                  packages=db.scalars(select(ServicePackage).where(ServicePackage.active.is_(True))).all(),
                  monthly_value=finance.monthly_value_cents)


@router.post("/clients/{cid}/onboarding/{code}")
def onboarding_step(cid: int, code: str, request: Request, status: str = Form(...), notes: str = Form(""), evidence: str = Form(""),
                    user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    _client(db, cid)
    try:
        onboarding.set_step(db, cid, code, status, user, notes or None, evidence or None)
        flash(request, "Step updated.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return _back(cid, "#onboarding")


@router.post("/clients/{cid}/automation/{feature}")
def automation(cid: int, feature: str, request: Request, enabled: str = Form(...), user: User = Depends(require("manager")),
               db: Session = Depends(get_db)):
    _client(db, cid)
    try:
        onboarding.set_automation(db, cid, feature, enabled == "1", user)
        flash(request, "Automation updated.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return _back(cid, "#automation")


def record_integration(db: Session, cid: int, kind: str, provider: str, status: str, note: str, user: User) -> IntegrationConnection:
    if kind not in CONNECTABLE_CHANNELS:
        raise ValueError("Unknown integration kind")
    if status not in ("not_connected", "sandbox", "connected", "error", "disabled"):
        raise ValueError("Invalid status")
    if status == "connected" and not note.strip():
        raise ValueError("Describe how the connection was authorized and verified before marking it connected")
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == cid, IntegrationConnection.kind == kind,
                                                         IntegrationConnection.provider == provider))
    if conn is None:
        conn = IntegrationConnection(client_id=cid, kind=kind, provider=provider)
        db.add(conn)
    conn.status, conn.authorization_note, conn.authorized_by_id = status, note.strip(), user.id
    conn.verified_at = utcnow() if status == "connected" else None
    db.flush()
    audit.record(db, "integration.status", actor=user, entity=conn, details={"kind": kind, "provider": provider, "status": status})
    return conn


@router.post("/clients/{cid}/integrations")
def integration(cid: int, request: Request, kind: str = Form(...), provider: str = Form(...), status: str = Form(...), note: str = Form(""),
                user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    _client(db, cid)
    try:
        record_integration(db, cid, kind, provider.strip()[:60], status, note, user)
        flash(request, "Integration status recorded.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return _back(cid, "#integrations")


@router.post("/clients/{cid}/agreements")
def agreement_create(cid: int, request: Request, name: str = Form(...), package_id: str = Form(""), start_date: str = Form(...),
                     setup_fee: str = Form(""), recurring_fee: str = Form(""), billing_frequency: str = Form("monthly"),
                     discount_percent: int = Form(0), custom_terms: str = Form(""), user: User = Depends(require("admin")),
                     db: Session = Depends(get_db)):
    c = _client(db, cid)
    try:
        pkg = db.get(ServicePackage, int(package_id)) if package_id else None
        a = finance.create_agreement(db, client=c, name=name, package=pkg, start_date=parse_date(start_date), actor=user,
                                     setup_fee_cents=parse_money(setup_fee) if setup_fee else None,
                                     recurring_fee_cents=parse_money(recurring_fee) if recurring_fee else None,
                                     billing_frequency=billing_frequency, discount_percent=discount_percent, custom_terms=custom_terms)
        flash(request, f"Agreement #{a.id} recorded as pending. Activate it once signed.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return _back(cid, "#agreements")


@router.post("/agreements/{aid}/{action}")
def agreement_action(aid: int, action: str, request: Request, reason: str = Form(""), period_start: str = Form(""),
                     user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    a = db.get(ClientAgreement, aid)
    if a is None:
        raise HTTPException(404, "Agreement not found")
    try:
        if action == "activate":
            finance.activate_agreement(db, a, user)
        elif action == "cancel":
            finance.cancel_agreement(db, a, user, utcnow().date(), reason)
        elif action == "invoice":
            today = utcnow().date()
            inv = finance.invoice_agreement_period(db, a, parse_date(period_start) if period_start else date(today.year, today.month, 1), user)
            flash(request, f"Draft invoice {inv.number} created. Review and issue it.")
            return RedirectResponse(f"/finance/invoices/{inv.id}", status_code=303)
        else:
            raise ValueError("Unknown action")
        flash(request, f"Agreement {action} recorded.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return _back(a.client_id, "#agreements")


@router.post("/clients/{cid}/users")
def client_user_create(cid: int, request: Request, email: str = Form(...), name: str = Form(""), password: str = Form(...),
                       user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    _client(db, cid)
    email = email.lower().strip()
    if db.scalar(select(User.id).where(User.email == email)):
        flash(request, "That email already has an account.", "error")
        return _back(cid, "#users")
    try:
        u = User(email=email, name=name or email.split("@")[0], role="client", client_id=cid, password_hash=hash_password(password))
    except ValueError as exc:
        flash(request, str(exc), "error")
        return _back(cid, "#users")
    db.add(u)
    db.flush()
    audit.record(db, "user.created", actor=user, client_id=cid, details={"user_id": u.id, "role": "client"})
    flash(request, f"Portal user {email} created.")
    return _back(cid, "#users")


@router.get("/tickets")
def tickets_page(request: Request, status: str = "", user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    q = select(SupportTicket).order_by(SupportTicket.id.desc())
    if status:
        q = q.where(SupportTicket.status == status)
    return render(request, "support_tickets.html", user, tickets=db.scalars(q.limit(500)).all(),
                  names=(names := {c.id: c.name for c in db.scalars(select(Client).order_by(Client.name))}), clients=names, kinds=tickets.KINDS, statuses=tickets.STATUSES, status=status)


@router.post("/tickets")
def ticket_create(request: Request, client_id: int = Form(...), kind: str = Form(...), title: str = Form(...), description: str = Form(""),
                  priority: str = Form("normal"), user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    _client(db, client_id)
    try:
        tickets.create_ticket(db, client_id=client_id, kind=kind, title=title, description=description, priority=priority, actor=user)
        flash(request, "Ticket created.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/tickets", status_code=303)


@router.post("/tickets/{tid}/status")
def ticket_status(tid: int, request: Request, status: str = Form(...), resolution: str = Form(""), user: User = Depends(require("rep")),
                  db: Session = Depends(get_db)):
    t = db.get(SupportTicket, tid)
    if t is None:
        raise HTTPException(404, "Ticket not found")
    try:
        tickets.update_status(db, t, status, user, resolution or None)
        flash(request, "Ticket updated.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/tickets", status_code=303)

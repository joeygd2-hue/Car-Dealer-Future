"""Server-rendered staff UI (/app). Forms post back here and call the same services as the API."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models.base import utcnow
from app.models.core import Attachment, AuditLog, Client, IntegrationConnection, SupportTicket
from app.models.costs import Alert, Budget
from app.models.finance import BankTransaction, ClientAgreement, Expense, ExpenseCategory, Invoice, ServicePackage
from app.models.outreach import AgentActivity, Campaign, InboundMessage, MessageTemplate, OutreachMessage, Prospect
from app.models.seo import ClientReport, SeoAudit, SeoIssue, SeoSite
from app.security.auth import AuthContext, require_admin, require_staff
from app.services import costs, finance, onboarding, packages, reports, tickets
from app.services.agent import dashboard as agent_dash
from app.services.agent import messaging
from app.services.agent.state import AgentError, set_state
from app.services.maintenance import attachment_path, store_attachment
from app.services.seo import audit as seo
from app.web.common import parse_date, parse_money, redirect, render

router = APIRouter(prefix="/app")


def _period(start: str | None, end: str | None) -> tuple[date, date]:
    today = utcnow().date()
    s = parse_date(start) if start else date(today.year, today.month, 1)
    e = parse_date(end) if end else today
    return s, e


def _months_back(d: date, n: int) -> date:
    idx = d.year * 12 + d.month - 1 - n
    return date(idx // 12, idx % 12 + 1, 1)


@router.get("", response_class=HTMLResponse)
def home(request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    today = utcnow().date()
    tiles = {
        "mrr": finance.mrr_cents(db, today) if ctx.user.is_admin else None,
        "clients_active": db.scalar(select(func.count()).select_from(Client).where(Client.status == "active")),
        "clients_onboarding": db.scalar(select(func.count()).select_from(Client).where(Client.status == "onboarding")),
        "open_tickets": db.scalar(select(func.count()).select_from(SupportTicket).where(SupportTicket.status.notin_(("resolved", "closed")))),
        "alerts": db.scalar(select(func.count()).select_from(Alert).where(Alert.acknowledged_at.is_(None))),
        "pending_approvals": db.scalar(select(func.count()).select_from(OutreachMessage).where(OutreachMessage.status == "pending_approval")),
    }
    agent = agent_dash.agent_dashboard(db, 7)["agent"]
    return render(request, "app/home.html", ctx, tiles=tiles, agent=agent)


# ----------------------------------------------------------------- clients --


@router.get("/clients", response_class=HTMLResponse)
def clients(request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    return render(request, "app/clients.html", ctx, clients=db.scalars(select(Client).order_by(Client.name)).all())


@router.post("/clients")
def create_client(name: str = Form(...), city: str = Form(""), phone: str = Form(""), website: str = Form(""), contact_email: str = Form(""),
                  ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    if not name.strip():
        return redirect("/app/clients", err="Name is required")
    c = Client(name=name.strip()[:200], city=city or None, phone=phone or None, website=website or None, contact_email=contact_email or None)
    db.add(c)
    db.flush()
    onboarding.ensure_checklist(db, c)
    db.commit()
    return redirect(f"/app/clients/{c.id}", msg="Client workspace created")


@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(client_id: int, request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    c = db.get(Client, client_id)
    if c is None:
        raise HTTPException(404, "Not found")
    steps = onboarding.ensure_checklist(db, c)
    db.commit()
    autos = {f: {"label": spec["label"], "enabled": onboarding.automation_enabled(db, c.id, f), "outstanding": onboarding.outstanding_prereqs(db, c.id, f)}
             for f, spec in onboarding.AUTOMATION_PREREQS.items()}
    return render(
        request, "app/client_detail.html", ctx, client=c, steps=steps, labels=onboarding.STEP_LABELS, automations=autos,
        integrations=db.scalars(select(IntegrationConnection).where(IntegrationConnection.client_id == c.id)).all(),
        agreements=db.scalars(select(ClientAgreement).where(ClientAgreement.client_id == c.id)).all(),
        tickets=db.scalars(select(SupportTicket).where(SupportTicket.client_id == c.id).order_by(SupportTicket.id.desc())).all(),
        packages=db.scalars(select(ServicePackage).where(ServicePackage.active.is_(True))).all(),
        monthly_value=finance.monthly_value_cents,
    )


@router.post("/clients/{client_id}/onboarding/{code}")
def set_step(client_id: int, code: str, status: str = Form(...), notes: str = Form(""), evidence: str = Form(""),
             ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    try:
        onboarding.set_step(db, client_id, code, status, ctx.user, notes or None, evidence or None)
        db.commit()
    except ValueError as exc:
        return redirect(f"/app/clients/{client_id}#onboarding", err=str(exc))
    return redirect(f"/app/clients/{client_id}#onboarding", msg="Step updated")


@router.post("/clients/{client_id}/automation/{feature}")
def set_automation(client_id: int, feature: str, enabled: str = Form(...), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        onboarding.set_automation(db, client_id, feature, enabled == "1", ctx.user)
        db.commit()
    except ValueError as exc:
        return redirect(f"/app/clients/{client_id}#automation", err=str(exc))
    return redirect(f"/app/clients/{client_id}#automation", msg="Automation updated")


@router.post("/clients/{client_id}/agreements")
def create_agreement(client_id: int, name: str = Form(...), package_id: str = Form(""), start_date: str = Form(...), setup_fee: str = Form(""),
                     recurring_fee: str = Form(""), billing_frequency: str = Form("monthly"), discount_percent: int = Form(0),
                     ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    c = db.get(Client, client_id)
    try:
        pkg = db.get(ServicePackage, int(package_id)) if package_id else None
        a = finance.create_agreement(db, client=c, name=name, package=pkg, start_date=parse_date(start_date), actor=ctx.user,
                                     setup_fee_cents=parse_money(setup_fee) if setup_fee else None, recurring_fee_cents=parse_money(recurring_fee) if recurring_fee else None,
                                     billing_frequency=billing_frequency, discount_percent=discount_percent)
        db.commit()
    except ValueError as exc:
        return redirect(f"/app/clients/{client_id}#agreements", err=str(exc))
    return redirect(f"/app/clients/{client_id}#agreements", msg=f"Agreement #{a.id} created (pending)")


@router.post("/agreements/{agreement_id}/{action}")
def agreement_action(agreement_id: int, action: str, reason: str = Form(""), period_start: str = Form(""), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    a = db.get(ClientAgreement, agreement_id)
    if a is None:
        raise HTTPException(404, "Not found")
    try:
        if action == "activate":
            finance.activate_agreement(db, a, ctx.user)
        elif action == "cancel":
            finance.cancel_agreement(db, a, ctx.user, utcnow().date(), reason)
        elif action == "invoice":
            inv = finance.invoice_agreement_period(db, a, parse_date(period_start) if period_start else date(utcnow().year, utcnow().month, 1), ctx.user)
            db.commit()
            return redirect(f"/app/finance/invoices/{inv.id}", msg="Draft invoice created")
        else:
            raise ValueError("Unknown action")
        db.commit()
    except ValueError as exc:
        return redirect(f"/app/clients/{a.client_id}#agreements", err=str(exc))
    return redirect(f"/app/clients/{a.client_id}#agreements", msg=f"Agreement {action}d")


@router.post("/clients/{client_id}/integrations")
def record_integration(client_id: int, kind: str = Form(...), provider: str = Form(...), status: str = Form(...), note: str = Form(""),
                       ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    if status == "connected" and not note.strip():
        return redirect(f"/app/clients/{client_id}#integrations", err="Describe how the connection was authorized and verified")
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == client_id, IntegrationConnection.kind == kind, IntegrationConnection.provider == provider))
    if conn is None:
        conn = IntegrationConnection(client_id=client_id, kind=kind, provider=provider, settings={})
        db.add(conn)
    conn.status, conn.authorization_note, conn.authorized_by_user_id = status, note or None, ctx.user.id
    conn.verified_at = utcnow() if status == "connected" else None
    db.commit()
    return redirect(f"/app/clients/{client_id}#integrations", msg="Integration status recorded")


# ----------------------------------------------------------------- tickets --


@router.get("/tickets", response_class=HTMLResponse)
def ticket_list(request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    rows = db.scalars(select(SupportTicket).order_by(SupportTicket.status, SupportTicket.id.desc()).limit(500)).all()
    names = {c.id: c.name for c in db.scalars(select(Client))}
    return render(request, "app/tickets.html", ctx, tickets=rows, names=names, clients=names, kinds=tickets.KINDS, statuses=tickets.STATUSES)


@router.post("/tickets")
def ticket_create(client_id: int = Form(...), kind: str = Form(...), title: str = Form(...), description: str = Form(""), priority: str = Form("normal"),
                  ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    try:
        tickets.create_ticket(db, client_id=client_id, kind=kind, title=title, description=description, priority=priority, actor=ctx.user)
        db.commit()
    except ValueError as exc:
        return redirect("/app/tickets", err=str(exc))
    return redirect("/app/tickets", msg="Ticket created")


@router.post("/tickets/{ticket_id}/status")
def ticket_status(ticket_id: int, status: str = Form(...), resolution: str = Form(""), ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    t = db.get(SupportTicket, ticket_id)
    if t is None:
        raise HTTPException(404, "Not found")
    try:
        tickets.update_status(db, t, status, ctx.user, resolution or None)
        db.commit()
    except ValueError as exc:
        return redirect("/app/tickets", err=str(exc))
    return redirect("/app/tickets", msg="Ticket updated")


# ----------------------------------------------------------------- finance --


@router.get("/finance", response_class=HTMLResponse)
def finance_overview(request: Request, start: str | None = None, end: str | None = None, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        s, e = _period(start, end)
        report = finance.financial_report(db, s, e)
    except ValueError as exc:
        return redirect("/app/finance", err=str(exc))
    flow = finance.cash_flow_trend(db, _months_back(e, 5), e)
    return render(request, "app/finance.html", ctx, r=report, start=s, end=e, flow=flow, suggested_fixed=finance.trailing_fixed_costs(db, e), scenario=None)


@router.post("/finance/break-even", response_class=HTMLResponse)
def break_even(request: Request, fixed: str = Form(...), price: str = Form(...), variable: str = Form("0"), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    s, e = _period(None, None)
    try:
        scen = finance.break_even_scenario(fixed_monthly_costs_cents=parse_money(fixed), price_per_client_cents=parse_money(price), variable_cost_per_client_cents=parse_money(variable))
    except ValueError as exc:
        return redirect("/app/finance", err=str(exc))
    flow = finance.cash_flow_trend(db, _months_back(e, 5), e)
    return render(request, "app/finance.html", ctx, r=finance.financial_report(db, s, e), start=s, end=e, flow=flow, suggested_fixed=finance.trailing_fixed_costs(db, e), scenario=scen)


@router.get("/finance/export/{kind}.csv")
def export(kind: str, start: str | None = None, end: str | None = None, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    s, e = _period(start, end)
    try:
        data = finance.export_csv(db, kind, s, e)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{kind}-{s}-{e}.csv"'})


@router.get("/finance/invoices", response_class=HTMLResponse)
def invoices(request: Request, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(Invoice).options(selectinload(Invoice.lines), selectinload(Invoice.payments), selectinload(Invoice.credits)).order_by(Invoice.issue_date.desc()).limit(500)).all()
    names = {c.id: c.name for c in db.scalars(select(Client))}
    return render(request, "app/invoices.html", ctx, invoices=rows, names=names, f=finance, today=utcnow().date())


@router.post("/finance/invoices")
def invoice_create(request: Request, client_id: int = Form(...), issue_date: str = Form(...), due_date: str = Form(...), kind: str = Form("one_time"),
                   description: str = Form(...), amount: str = Form(...), discount: str = Form(""), notes: str = Form(""),
                   ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    c = db.get(Client, client_id)
    if c is None:
        return redirect("/app/finance/invoices", err="Unknown client")
    try:
        lines = [finance.LineInput(kind, description, parse_money(amount))]
        if discount:
            lines.append(finance.LineInput("discount", "Discount", -parse_money(discount)))
        inv = finance.create_invoice(db, client=c, issue_date=parse_date(issue_date), due_date=parse_date(due_date), lines=lines, actor=ctx.user, notes=notes or None)
        db.commit()
    except ValueError as exc:
        return redirect("/app/finance/invoices", err=str(exc))
    return redirect(f"/app/finance/invoices/{inv.id}", msg="Draft invoice created")


@router.get("/finance/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(invoice_id: int, request: Request, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Not found")
    return render(request, "app/invoice_detail.html", ctx, inv=inv, client=db.get(Client, inv.client_id), f=finance, methods=finance.PAYMENT_METHODS, today=utcnow().date())


@router.post("/finance/invoices/{invoice_id}/{action}")
def invoice_action(invoice_id: int, action: str, reason: str = Form(""), amount: str = Form(""), received_date: str = Form(""), method: str = Form("ach"),
                   reference: str = Form(""), kind: str = Form("credit"), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Not found")
    try:
        if action == "issue":
            finance.issue_invoice(db, inv, ctx.user)
        elif action == "void":
            finance.void_invoice(db, inv, ctx.user, reason)
        elif action == "write_off":
            finance.write_off_invoice(db, inv, ctx.user, reason)
        elif action == "payment":
            finance.record_payment(db, client_id=inv.client_id, invoice=inv, amount_cents=parse_money(amount), received_date=parse_date(received_date, "received date"),
                                   method=method, reference=reference or None, actor=ctx.user)
        elif action == "credit":
            finance.record_credit_note(db, client_id=inv.client_id, invoice=inv, kind=kind, amount_cents=parse_money(amount), issued_date=utcnow().date(), reason=reason, actor=ctx.user)
        else:
            raise ValueError("Unknown action")
        db.commit()
    except ValueError as exc:
        return redirect(f"/app/finance/invoices/{invoice_id}", err=str(exc))
    return redirect(f"/app/finance/invoices/{invoice_id}", msg="Saved")


@router.get("/finance/expenses", response_class=HTMLResponse)
def expenses(request: Request, start: str | None = None, end: str | None = None, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    s, e = _period(start, end)
    rows = db.scalars(select(Expense).options(selectinload(Expense.category)).where(Expense.expense_date.between(s, e)).order_by(Expense.expense_date.desc())).all()
    receipts = {a.owner_id: a for a in db.scalars(select(Attachment).where(Attachment.owner_type == "expense"))}
    return render(request, "app/expenses.html", ctx, expenses=rows, receipts=receipts, start=s, end=e,
                  categories=db.scalars(select(ExpenseCategory).where(ExpenseCategory.active.is_(True)).order_by(ExpenseCategory.name)).all(),
                  clients=db.scalars(select(Client).order_by(Client.name)).all(), disclaimer=finance.TAX_DISCLAIMER, today=utcnow().date())


@router.post("/finance/expenses")
async def expense_create(expense_date: str = Form(...), vendor: str = Form(...), category_id: int = Form(...), amount: str = Form(...), currency: str = Form("USD"),
                         business_purpose: str = Form(...), recurrence: str = Form("one_time"), client_id: str = Form(""), project: str = Form(""),
                         reimbursement_status: str = Form("not_applicable"), notes: str = Form(""), receipt: UploadFile | None = File(None),
                         ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        e = finance.create_expense(db, expense_date=parse_date(expense_date), vendor=vendor, category_id=category_id, amount_cents=parse_money(amount), currency=currency,
                                   business_purpose=business_purpose, recurrence=recurrence, client_id=int(client_id) if client_id else None, project=project or None,
                                   reimbursement_status=reimbursement_status, notes=notes or None, actor=ctx.user)
        if receipt is not None and receipt.filename:
            store_attachment(db, owner_type="expense", owner_id=e.id, filename=receipt.filename, content_type=receipt.content_type or "", data=await receipt.read(), actor=ctx.user)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return redirect("/app/finance/expenses", err=str(exc))
    return redirect("/app/finance/expenses", msg="Expense recorded (manual entry)")


@router.get("/attachments/{att_id}")
def attachment(att_id: int, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    att = db.get(Attachment, att_id)
    if att is None:
        raise HTTPException(404, "Not found")
    return FileResponse(attachment_path(att), media_type=att.content_type, filename=att.filename, headers={"X-Content-Type-Options": "nosniff"})


@router.get("/finance/reconciliation", response_class=HTMLResponse)
def reconciliation(request: Request, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    txs = db.scalars(select(BankTransaction).where(BankTransaction.status == "unmatched").order_by(BankTransaction.posted_date)).all()
    return render(request, "app/reconciliation.html", ctx, txs=[(t, finance.suggest_matches(db, t)) for t in txs])


@router.post("/finance/reconciliation/import")
async def reconciliation_import(file: UploadFile = File(...), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    res = finance.import_bank_csv(db, (await file.read()).decode("utf-8", "replace"))
    db.commit()
    return redirect("/app/finance/reconciliation", msg=f"Imported {res['created']} lines ({res['duplicates']} duplicates skipped, {len(res['errors'])} errors)")


@router.post("/finance/reconciliation/{tx_id}")
def reconciliation_match(tx_id: int, match: str = Form(...), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    tx = db.get(BankTransaction, tx_id)
    try:
        mtype, mid = match.split(":")
        finance.confirm_match(db, tx, mtype, int(mid), ctx.user)
        db.commit()
    except (ValueError, AttributeError) as exc:
        return redirect("/app/finance/reconciliation", err=str(exc))
    return redirect("/app/finance/reconciliation", msg="Matched")


# ------------------------------------------------------------- packages ----


@router.get("/packages", response_class=HTMLResponse)
def package_list(request: Request, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(ServicePackage).order_by(ServicePackage.id)).all()
    return render(request, "app/packages.html", ctx, packages=rows, all_features=lambda p: packages.all_features(db, p))


@router.post("/packages/{pkg_id}")
def package_update(pkg_id: int, name: str = Form(...), setup_fee: str = Form("0"), recurring_fee: str = Form("0"), billing_frequency: str = Form("monthly"),
                   pricing_status: str = Form("proposed"), features: str = Form(""), active: str = Form(""), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    p = db.get(ServicePackage, pkg_id)
    if p is None:
        raise HTTPException(404, "Not found")
    try:
        packages.update_package(db, p, {"name": name, "setup_fee_cents": parse_money(setup_fee), "recurring_fee_cents": parse_money(recurring_fee),
                                        "billing_frequency": billing_frequency, "pricing_status": pricing_status,
                                        "features": [x.strip() for x in features.splitlines() if x.strip()], "active": active == "1"}, ctx.user)
        db.commit()
    except ValueError as exc:
        return redirect("/app/packages", err=str(exc))
    return redirect("/app/packages", msg="Package saved")


# ------------------------------------------------------------------- costs --


@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, start: str | None = None, end: str | None = None, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    s, e = _period(start, end)
    return render(request, "app/costs.html", ctx, usage=costs.usage_summary(db, s, e), start=s, end=e, budgets=db.scalars(select(Budget)).all(),
                  alerts=db.scalars(select(Alert).order_by(Alert.id.desc()).limit(50)).all())


@router.post("/costs/budgets")
def budget_create(name: str = Form(...), provider: str = Form(""), service_category: str = Form(""), period: str = Form("monthly"), amount: str = Form(...),
                  threshold: int = Form(80), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        db.add(Budget(name=name, provider=provider or None, service_category=service_category or None, period=period, amount_micros=parse_money(amount) * 10_000, alert_threshold_percent=threshold))
        db.commit()
    except ValueError as exc:
        return redirect("/app/costs", err=str(exc))
    return redirect("/app/costs", msg="Budget saved")


@router.post("/alerts/{alert_id}/ack")
def alert_ack(alert_id: int, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if a:
        a.acknowledged_at, a.acknowledged_by_user_id = utcnow(), ctx.user.id
        db.commit()
    return redirect("/app/costs", msg="Alert acknowledged")


# --------------------------------------------------------------------- SEO --


@router.get("/seo", response_class=HTMLResponse)
def seo_page(request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    sites = db.scalars(select(SeoSite).order_by(SeoSite.id)).all()
    latest = {s.id: db.scalar(select(SeoAudit).where(SeoAudit.site_id == s.id).order_by(SeoAudit.id.desc()).limit(1)) for s in sites}
    names = {c.id: c.name for c in db.scalars(select(Client))}
    return render(request, "app/seo.html", ctx, sites=sites, latest=latest, names=names)


@router.post("/seo/sites")
def seo_site_create(client_id: int = Form(...), base_url: str = Form(...), max_pages: int = Form(50), ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    if not base_url.startswith(("http://", "https://")):
        return redirect("/app/seo", err="URL must start with http:// or https://")
    db.add(SeoSite(client_id=client_id, base_url=base_url.strip(), max_pages=max(1, min(max_pages, 500))))
    db.commit()
    return redirect("/app/seo", msg="Site added (not yet authorized)")


@router.post("/seo/sites/{site_id}/authorize")
def seo_authorize(site_id: int, note: str = Form(...), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        seo.authorize_site(db, db.get(SeoSite, site_id), ctx.user, note)
        db.commit()
    except (ValueError, AttributeError) as exc:
        return redirect("/app/seo", err=str(exc))
    return redirect("/app/seo", msg="Site authorized")


@router.post("/seo/sites/{site_id}/audit")
def seo_run(site_id: int, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    try:
        a = seo.start_audit(db, db.get(SeoSite, site_id), ctx.user)
        db.commit()
    except (ValueError, AttributeError) as exc:
        return redirect("/app/seo", err=str(exc))
    return redirect("/app/seo", msg=f"Audit #{a.id} queued; the worker will run it")


@router.get("/seo/clients/{client_id}", response_class=HTMLResponse)
def seo_issues(client_id: int, request: Request, status: str | None = None, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    stmt = select(SeoIssue).where(SeoIssue.client_id == client_id)
    if status:
        stmt = stmt.where(SeoIssue.status == status)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    rows = sorted(db.scalars(stmt).all(), key=lambda i: (i.status != "open", order.get(i.severity, 9)))
    return render(request, "app/seo_issues.html", ctx, issues=rows, client=db.get(Client, client_id), status=status)


@router.post("/seo/issues/{issue_id}")
def seo_issue_status(issue_id: int, status: str = Form(...), ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    i = db.get(SeoIssue, issue_id)
    if i is None:
        raise HTTPException(404, "Not found")
    try:
        seo.update_issue_status(db, i, status, ctx.user)
        db.commit()
    except ValueError as exc:
        return redirect(f"/app/seo/clients/{i.client_id}", err=str(exc))
    return redirect(f"/app/seo/clients/{i.client_id}", msg="Issue updated")


# ----------------------------------------------------------------- reports --


@router.get("/reports", response_class=HTMLResponse)
def report_list(request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    rows = db.scalars(select(ClientReport).order_by(ClientReport.id.desc()).limit(200)).all()
    clients = db.scalars(select(Client).order_by(Client.name)).all()
    ps, pe = reports.previous_month(utcnow().date())
    return render(request, "app/reports.html", ctx, reports=rows, clients=clients, names={c.id: c.name for c in clients}, ps=ps, pe=pe)


@router.post("/reports")
def report_create(client_id: int = Form(...), period_start: str = Form(...), period_end: str = Form(...), ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    c = db.get(Client, client_id)
    try:
        r = reports.generate_report(db, c, parse_date(period_start), parse_date(period_end), ctx.user)
        db.commit()
    except (ValueError, AttributeError) as exc:
        return redirect("/app/reports", err=str(exc))
    return redirect(f"/app/reports/{r.id}", msg="Report generated")


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report_view(report_id: int, request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    r = db.get(ClientReport, report_id)
    if r is None:
        raise HTTPException(404, "Not found")
    return render(request, "report.html", ctx, report=r, d=r.data, staff=True, client=db.get(Client, r.client_id))


@router.post("/reports/{report_id}/send")
def report_send(report_id: int, to: str = Form(...), ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    r = db.get(ClientReport, report_id)
    if r is None or "@" not in to:
        return redirect(f"/app/reports/{report_id}", err="Invalid recipient")
    status = reports.send_report(db, r, to.strip(), ctx.user)
    db.commit()
    note = "Sent through the authorized provider" if status == "sent" else "Held in the sandbox outbox: no authorized report-email integration for this client"
    return redirect(f"/app/reports/{report_id}", msg=note)


@router.get("/reports/{report_id}/download")
def report_download(report_id: int, request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    r = db.get(ClientReport, report_id)
    if r is None:
        raise HTTPException(404, "Not found")
    html = render(request, "report.html", ctx, report=r, d=r.data, staff=False, client=db.get(Client, r.client_id)).body
    return Response(html, media_type="text/html", headers={"Content-Disposition": f'attachment; filename="report-{r.client_id}-{r.period_start}.html"'})


# ------------------------------------------------------------------- agent --


@router.get("/agent", response_class=HTMLResponse)
def agent_page(request: Request, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    d = agent_dash.agent_dashboard(db, 30)
    pending = db.scalars(select(OutreachMessage).where(OutreachMessage.status == "pending_approval").order_by(OutreachMessage.id).limit(50)).all()
    human = db.scalars(select(InboundMessage).where(InboundMessage.needs_human.is_(True)).order_by(InboundMessage.id.desc()).limit(50)).all()
    activity = db.scalars(select(AgentActivity).order_by(AgentActivity.id.desc()).limit(50)).all()
    prospects = db.scalars(select(Prospect).order_by(Prospect.priority.desc()).limit(50)).all()
    return render(request, "app/agent.html", ctx, d=d, pending=pending, human=human, activity=activity, prospects=prospects,
                  campaigns=db.scalars(select(Campaign)).all(), templates=db.scalars(select(MessageTemplate)).all())


@router.post("/agent/state")
def agent_state(state: str = Form(...), reason: str = Form(""), ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    if state == "running" and not ctx.user.is_admin:
        return redirect("/app/agent", err="Only an admin can start the agent")
    try:
        set_state(db, state, ctx.user, reason)
        db.commit()
    except AgentError as exc:
        return redirect("/app/agent", err=str(exc))
    return redirect("/app/agent", msg=f"Agent {state}")


@router.post("/agent/messages/{msg_id}/{decision}")
def agent_decide(msg_id: int, decision: str, body: str = Form(""), reason: str = Form("rejected by reviewer"), ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    m = db.get(OutreachMessage, msg_id)
    if m is None:
        raise HTTPException(404, "Not found")
    try:
        if decision == "approve":
            messaging.approve_message(db, m, ctx.user, body=body if body and body != m.body else None)
        else:
            messaging.reject_message(db, m, ctx.user, reason)
        db.commit()
    except messaging.MessagingError as exc:
        return redirect("/app/agent", err=str(exc))
    return redirect("/app/agent#approvals", msg=f"Message {decision}d")


@router.post("/agent/inbound/{inbound_id}/resolve")
def agent_resolve(inbound_id: int, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    m = db.get(InboundMessage, inbound_id)
    if m:
        m.needs_human = False
        db.commit()
    return redirect("/app/agent#replies", msg="Marked as handled")


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "app/audit.html", ctx, rows=db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)).all())

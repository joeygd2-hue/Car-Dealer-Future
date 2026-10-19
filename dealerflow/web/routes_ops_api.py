"""JSON API for Phase 2-4 (staff). Auth: session cookie + X-CSRF-Token, or ``Authorization: Bearer <api key>``.

Role rules: viewer reads operations data; rep runs day-to-day actions; manager records integrations,
authorizations and automations and reads finance; admin changes finance and the service catalog.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db, utcnow
from ..models import (
    Alert,
    Budget,
    Client,
    ClientAgreement,
    ClientReport,
    Expense,
    ExpenseCategory,
    Invoice,
    Prospect,
    SeoAudit,
    SeoIssue,
    SeoSite,
    ServicePackage,
    SupportTicket,
    User,
    Vehicle,
)
from ..services import client_reports, finance, onboarding, packages, seo_audit, tenancy, tickets, usage
from ..services.conversations import channels, processing, runtime
from .deps import check_csrf, require
from .routes_clients import record_integration
from .routes_seo import import_search_csv

router = APIRouter(prefix="/api/v1", dependencies=[Depends(check_csrf)])


def bad(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


def _get(db: Session, model, oid: int, client_id: int | None = None):
    obj = db.get(model, oid)
    if obj is None or (client_id is not None and getattr(obj, "client_id", None) != client_id):
        raise HTTPException(404, "Not found")
    return obj


def _period(start: date | None, end: date | None) -> tuple[date, date]:
    today = utcnow().date()
    return start or date(today.year, today.month, 1), end or today


# --- clients, onboarding, integrations, tickets ---------------------------------------------


class ClientIn(BaseModel):
    name: str = Field("", max_length=255)
    prospect_id: int | None = None
    city: str | None = None
    phone: str | None = None
    website: str | None = None
    contact_email: EmailStr | None = None


def client_out(c: Client) -> dict:
    return {k: getattr(c, k) for k in ("id", "name", "status", "prospect_id", "city", "phone", "website", "contact_email")}


@router.get("/clients")
def clients(user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    return [client_out(c) for c in db.scalars(select(Client).order_by(Client.name))]


@router.post("/clients", status_code=201)
def client_create(body: ClientIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    prospect = _get(db, Prospect, body.prospect_id) if body.prospect_id else None
    if prospect is not None and prospect.stage != "won":
        raise HTTPException(409, "Only a won prospect can become a client")
    name = body.name.strip() or (prospect.name if prospect else "")
    if not name:
        raise HTTPException(400, "Name is required")
    extra = {k: v for k, v in body.model_dump(exclude={"name", "prospect_id"}).items() if v}
    return client_out(tenancy.create_client(db, name=name, actor=user, prospect=prospect, **extra))


@router.get("/clients/{cid}/onboarding")
def onboarding_get(cid: int, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    c = _get(db, Client, cid)
    steps = onboarding.ensure_checklist(db, c)
    return {"steps": [{"code": s.code, "label": onboarding.STEP_LABELS[s.code], "status": s.status, "evidence": s.evidence} for s in steps],
            "automations": {f: {"enabled": onboarding.automation_enabled(db, cid, f), "outstanding": onboarding.outstanding_prereqs(db, cid, f)}
                            for f in onboarding.AUTOMATION_PREREQS}}


class StepIn(BaseModel):
    status: str
    notes: str | None = None
    evidence: str | None = None


@router.post("/clients/{cid}/onboarding/{code}")
def onboarding_set(cid: int, code: str, body: StepIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    _get(db, Client, cid)
    try:
        s = onboarding.set_step(db, cid, code, body.status, user, body.notes, body.evidence)
    except ValueError as exc:
        raise bad(exc) from exc
    return {"code": s.code, "status": s.status}


class ToggleIn(BaseModel):
    enabled: bool


@router.post("/clients/{cid}/automation/{feature}")
def automation_set(cid: int, feature: str, body: ToggleIn, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    _get(db, Client, cid)
    try:
        row = onboarding.set_automation(db, cid, feature, body.enabled, user)
    except ValueError as exc:
        raise bad(exc) from exc
    return {"feature": row.feature, "enabled": row.enabled}


class IntegrationIn(BaseModel):
    kind: str
    provider: str = Field(min_length=1, max_length=60)
    status: str
    note: str = ""


@router.post("/clients/{cid}/integrations")
def integration_set(cid: int, body: IntegrationIn, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    _get(db, Client, cid)
    try:
        conn = record_integration(db, cid, body.kind, body.provider, body.status, body.note, user)
    except ValueError as exc:
        raise bad(exc) from exc
    return {"id": conn.id, "kind": conn.kind, "status": conn.status}


class TicketIn(BaseModel):
    client_id: int
    kind: str
    title: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=10000)
    priority: str = "normal"


def ticket_out(t: SupportTicket) -> dict:
    return {k: getattr(t, k) for k in ("id", "client_id", "kind", "title", "priority", "status", "source")}


@router.get("/tickets")
def ticket_list(status: str | None = None, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    q = select(SupportTicket).order_by(SupportTicket.id.desc())
    if status:
        q = q.where(SupportTicket.status == status)
    return [ticket_out(t) for t in db.scalars(q.limit(500))]


@router.post("/tickets", status_code=201)
def ticket_create(body: TicketIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    _get(db, Client, body.client_id)
    try:
        return ticket_out(tickets.create_ticket(db, actor=user, **body.model_dump()))
    except ValueError as exc:
        raise bad(exc) from exc


class CsvIn(BaseModel):
    csv: str = Field(max_length=5_000_000)


# --- customer conversations (staff test tool; the inbox itself is /dealers/{id}/inbox and /api/v1/dealers/{id}) ---


class InquiryIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    channel: str = "WEBSITE_CHAT"
    visitor_id: str = Field(default="staff-test-visitor", min_length=8, max_length=64)
    provider_message_id: str = Field(min_length=1, max_length=200)
    customer_name: str | None = Field(default=None, max_length=100)
    vehicle_id: int | None = None


@router.post("/clients/{cid}/inquiries/simulated", status_code=201)
def inquiry_simulated(cid: int, body: InquiryIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    """Inject a SIMULATED car-buyer message into the real pipeline (drafted by the worker; never reported as real)."""
    c = _get(db, Client, cid)
    if body.channel not in channels.CUSTOMER_CHANNELS:
        raise HTTPException(400, "Unknown channel")
    vehicle_id = _get(db, Vehicle, body.vehicle_id, cid).id if body.vehicle_id else None
    try:
        res = processing.ingest_inbound(db, dealership=c, channel=body.channel, external_conversation_id=f"web:{body.visitor_id}",
                                        text=body.text, provider_message_id=f"staff-test:{body.provider_message_id}",
                                        contact_hints={"name": body.customer_name}, vehicle_id=vehicle_id, simulated=True)
    except ValueError as exc:
        raise bad(exc) from exc
    runtime.record(db, "conversation.simulated_inquiry", actor_type="USER", actor_user_id=user.id, client_id=cid,
                   entity_type="message", entity_id=res.message.id)
    return {"conversation_id": res.conversation.id, "message_id": res.message.id, "created": res.created,
            "simulated": res.conversation.simulated}


# --- finance --------------------------------------------------------------------------------------


@router.get("/packages")
def package_list(user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    return [{"id": p.id, "code": p.code, "name": p.name, "setup_fee_cents": p.setup_fee_cents, "recurring_fee_cents": p.recurring_fee_cents,
             "billing_frequency": p.billing_frequency, "pricing_status": p.pricing_status, "active": p.active,
             "all_features": packages.all_features(db, p)} for p in db.scalars(select(ServicePackage).order_by(ServicePackage.id))]


@router.patch("/packages/{pid}")
def package_update(pid: int, changes: dict, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    p = _get(db, ServicePackage, pid)
    try:
        packages.update_package(db, p, changes, user)
    except ValueError as exc:
        raise bad(exc) from exc
    return {"id": p.id, "setup_fee_cents": p.setup_fee_cents, "recurring_fee_cents": p.recurring_fee_cents, "pricing_status": p.pricing_status}


class AgreementIn(BaseModel):
    client_id: int
    package_id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    start_date: date
    end_date: date | None = None
    setup_fee_cents: int | None = Field(None, ge=0)
    recurring_fee_cents: int | None = Field(None, ge=0)
    billing_frequency: str | None = None
    discount_percent: int = Field(0, ge=0, le=100)
    discount_cents: int = Field(0, ge=0)
    custom_terms: str | None = None


def agreement_out(a: ClientAgreement) -> dict:
    return {k: getattr(a, k) for k in ("id", "client_id", "name", "status", "start_date", "setup_fee_cents", "recurring_fee_cents",
                                         "billing_frequency", "discount_percent", "cancelled_at")} | {"monthly_value_cents": finance.monthly_value_cents(a)}


@router.post("/agreements", status_code=201)
def agreement_create(body: AgreementIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    c = _get(db, Client, body.client_id)
    pkg = _get(db, ServicePackage, body.package_id) if body.package_id else None
    try:
        return agreement_out(finance.create_agreement(db, client=c, package=pkg, actor=user, **body.model_dump(exclude={"client_id", "package_id"})))
    except ValueError as exc:
        raise bad(exc) from exc


class AgreementActionIn(BaseModel):
    action: str
    effective_date: date | None = None
    reason: str = ""
    period_start: date | None = None


@router.post("/agreements/{aid}/actions")
def agreement_action(aid: int, body: AgreementActionIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    a = _get(db, ClientAgreement, aid)
    try:
        if body.action == "activate":
            finance.activate_agreement(db, a, user)
        elif body.action == "cancel":
            finance.cancel_agreement(db, a, user, body.effective_date or utcnow().date(), body.reason)
        elif body.action == "invoice":
            today = utcnow().date()
            return invoice_out(finance.invoice_agreement_period(db, a, body.period_start or date(today.year, today.month, 1), user))
        else:
            raise ValueError("Unknown action")
    except ValueError as exc:
        raise bad(exc) from exc
    return agreement_out(a)


def invoice_out(inv: Invoice) -> dict:
    return {"id": inv.id, "number": inv.number, "client_id": inv.client_id, "status": inv.status, "display_status": finance.invoice_display_status(inv),
            "issue_date": inv.issue_date, "due_date": inv.due_date, "source": inv.source, "total_cents": finance.invoice_total(inv),
            "paid_cents": finance.invoice_paid(inv), "credited_cents": finance.invoice_credited(inv), "balance_cents": finance.invoice_balance(inv),
            "lines": [{"kind": li.kind, "description": li.description, "amount_cents": li.amount_cents} for li in inv.lines]}


class LineIn(BaseModel):
    kind: str
    description: str = Field(min_length=1, max_length=300)
    unit_amount_cents: int
    quantity: int = Field(1, gt=0)


class InvoiceIn(BaseModel):
    client_id: int
    issue_date: date
    due_date: date
    lines: list[LineIn] = Field(min_length=1)
    notes: str | None = None
    issue: bool = False


@router.get("/invoices")
def invoice_list(client_id: int | None = None, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    q = select(Invoice).options(selectinload(Invoice.lines), selectinload(Invoice.payments), selectinload(Invoice.credits)).order_by(Invoice.issue_date.desc())
    if client_id:
        q = q.where(Invoice.client_id == client_id)
    return [invoice_out(i) for i in db.scalars(q.limit(1000))]


@router.post("/invoices", status_code=201)
def invoice_create(body: InvoiceIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    c = _get(db, Client, body.client_id)
    try:
        inv = finance.create_invoice(db, client=c, issue_date=body.issue_date, due_date=body.due_date, actor=user, notes=body.notes,
                                     lines=[finance.LineInput(**li.model_dump()) for li in body.lines])
        if body.issue:
            finance.issue_invoice(db, inv, user)
    except ValueError as exc:
        raise bad(exc) from exc
    return invoice_out(inv)


class InvoiceActionIn(BaseModel):
    action: str
    reason: str = ""


@router.post("/invoices/{iid}/actions")
def invoice_action(iid: int, body: InvoiceActionIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    inv = _get(db, Invoice, iid)
    try:
        {"issue": lambda: finance.issue_invoice(db, inv, user), "void": lambda: finance.void_invoice(db, inv, user, body.reason),
         "write_off": lambda: finance.write_off_invoice(db, inv, user, body.reason)}[body.action]()
    except KeyError as exc:
        raise HTTPException(400, "Unknown action") from exc
    except ValueError as exc:
        raise bad(exc) from exc
    return invoice_out(inv)


class PaymentIn(BaseModel):
    client_id: int
    invoice_id: int | None = None
    amount_cents: int = Field(gt=0)
    received_date: date | None = None
    method: str = "ach"
    status: str = Field("confirmed", pattern="^(confirmed|pending)$")
    reference: str | None = Field(None, max_length=200)
    notes: str | None = None


@router.post("/payments", status_code=201)
def payment_create(body: PaymentIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    _get(db, Client, body.client_id)
    inv = _get(db, Invoice, body.invoice_id) if body.invoice_id else None
    try:
        p = finance.record_payment(db, invoice=inv, actor=user, **body.model_dump(exclude={"invoice_id"}))
    except ValueError as exc:
        raise bad(exc) from exc
    return {"id": p.id, "status": p.status, "amount_cents": p.amount_cents, "source": p.source}


class CreditIn(BaseModel):
    client_id: int
    invoice_id: int | None = None
    kind: str
    amount_cents: int = Field(gt=0)
    issued_date: date
    reason: str = Field(min_length=1)


@router.post("/credit-notes", status_code=201)
def credit_create(body: CreditIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    _get(db, Client, body.client_id)
    inv = _get(db, Invoice, body.invoice_id) if body.invoice_id else None
    try:
        c = finance.record_credit_note(db, invoice=inv, actor=user, **body.model_dump(exclude={"invoice_id"}))
    except ValueError as exc:
        raise bad(exc) from exc
    return {"id": c.id, "kind": c.kind, "amount_cents": c.amount_cents}


@router.get("/expense-categories")
def categories(user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    return [{"id": c.id, "code": c.code, "name": c.name, "is_acquisition": c.is_acquisition} for c in db.scalars(select(ExpenseCategory).order_by(ExpenseCategory.name))]


class CategoryIn(BaseModel):
    code: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    name: str = Field(min_length=1, max_length=100)
    is_acquisition: bool = False


@router.post("/expense-categories", status_code=201)
def category_create(body: CategoryIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    if db.scalar(select(ExpenseCategory.id).where(ExpenseCategory.code == body.code)):
        raise HTTPException(409, "Code exists")
    c = ExpenseCategory(**body.model_dump())
    db.add(c)
    db.flush()
    return {"id": c.id, "code": c.code}


class ExpenseIn(BaseModel):
    expense_date: date
    vendor: str = Field(min_length=1, max_length=200)
    category_id: int
    amount_cents: int = Field(gt=0)
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    business_purpose: str = Field(min_length=1)
    recurrence: str = "one_time"
    client_id: int | None = None
    project: str | None = None
    reimbursement_status: str = "not_applicable"
    notes: str | None = None


@router.get("/expenses")
def expense_list(start: date | None = None, end: date | None = None, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    s, e = _period(start, end)
    return [{k: getattr(x, k) for k in ("id", "expense_date", "vendor", "category_id", "amount_cents", "currency", "recurrence", "client_id", "source", "reconciled")}
            for x in db.scalars(select(Expense).where(Expense.expense_date.between(s, e)).order_by(Expense.expense_date.desc()))]


@router.post("/expenses", status_code=201)
def expense_create(body: ExpenseIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    if body.client_id:
        _get(db, Client, body.client_id)
    try:
        e = finance.create_expense(db, actor=user, **body.model_dump())
    except ValueError as exc:
        raise bad(exc) from exc
    return {"id": e.id, "amount_cents": e.amount_cents, "source": e.source}


@router.get("/finance/report")
def finance_report(start: date | None = None, end: date | None = None, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    s, e = _period(start, end)
    try:
        return finance.financial_report(db, s, e)
    except ValueError as exc:
        raise bad(exc) from exc


@router.get("/finance/cash-flow")
def cash_flow(start: date | None = None, end: date | None = None, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    today = utcnow().date()
    return finance.cash_flow_trend(db, start or date(today.year - 1, today.month, 1), end or today)


class ScenarioIn(BaseModel):
    fixed_monthly_costs_cents: int | None = Field(None, ge=0)
    price_per_client_cents: int = Field(ge=0)
    variable_cost_per_client_cents: int = Field(0, ge=0)


@router.post("/finance/break-even")
def break_even(body: ScenarioIn, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    fixed = body.fixed_monthly_costs_cents if body.fixed_monthly_costs_cents is not None else finance.trailing_fixed_costs(db, utcnow().date())
    return finance.break_even_scenario(fixed_monthly_costs_cents=fixed, price_per_client_cents=body.price_per_client_cents,
                                       variable_cost_per_client_cents=body.variable_cost_per_client_cents)


@router.get("/finance/export/{kind}.csv")
def finance_export(kind: str, start: date | None = None, end: date | None = None, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    s, e = _period(start, end)
    try:
        data = finance.export_csv(db, kind, s, e)
    except ValueError as exc:
        raise bad(exc) from exc
    return Response(data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{kind}-{s}-{e}.csv"'})


@router.post("/finance/bank-import")
def bank_import(body: CsvIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    return finance.import_bank_csv(db, body.csv)


@router.get("/finance/reconciliation")
def reconciliation(user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    from ..models import BankTransaction

    return [{"id": t.id, "date": t.posted_date, "description": t.description, "amount_cents": t.amount_cents, "suggestions": finance.suggest_matches(db, t)}
            for t in db.scalars(select(BankTransaction).where(BankTransaction.status == "unmatched").order_by(BankTransaction.posted_date))]


class MatchIn(BaseModel):
    match_type: str
    match_id: int


@router.post("/finance/reconciliation/{tx_id}")
def reconciliation_match(tx_id: int, body: MatchIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    from ..models import BankTransaction

    tx = _get(db, BankTransaction, tx_id)
    try:
        finance.confirm_match(db, tx, body.match_type, body.match_id, user)
    except ValueError as exc:
        raise bad(exc) from exc
    return {"id": tx.id, "status": tx.status}


# --- usage costs and alerts -------------------------------------------------------------------


@router.get("/costs/usage")
def costs_usage(start: date | None = None, end: date | None = None, client_id: int | None = None, user: User = Depends(require("manager")),
                db: Session = Depends(get_db)):
    s, e = _period(start, end)
    return usage.summary(db, s, e, client_id)


class BudgetIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str | None = None
    period: str = Field("monthly", pattern="^(daily|monthly)$")
    amount_cents: int = Field(gt=0)
    alert_threshold_percent: int = Field(80, ge=1, le=100)
    spike_multiplier: int = Field(3, ge=2, le=50)


@router.post("/costs/budgets", status_code=201)
def budget_create(body: BudgetIn, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    b = Budget(**body.model_dump())
    db.add(b)
    db.flush()
    return {"id": b.id}


@router.post("/costs/check-budgets")
def budget_check(user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    return {"alerts": usage.check_budgets(db)}


@router.get("/alerts")
def alert_list(user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    return [{"id": a.id, "kind": a.kind, "severity": a.severity, "message": a.message, "acknowledged": a.acknowledged_at is not None}
            for a in db.scalars(select(Alert).order_by(Alert.id.desc()).limit(200))]


# --- SEO and client reports ----------------------------------------------------------------------


class SiteIn(BaseModel):
    client_id: int
    base_url: str = Field(pattern=r"^https?://[^\s/]+", max_length=500)
    max_pages: int = Field(50, ge=1, le=500)
    crawl_delay_seconds: float = Field(1.0, ge=0.5, le=30)


@router.post("/seo/sites", status_code=201)
def site_create(body: SiteIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    _get(db, Client, body.client_id)
    site = SeoSite(**body.model_dump())
    db.add(site)
    db.flush()
    return {"id": site.id, "authorized": site.authorized}


class NoteIn(BaseModel):
    note: str = Field(min_length=5)


@router.post("/seo/sites/{sid}/authorize")
def site_authorize(sid: int, body: NoteIn, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    site = _get(db, SeoSite, sid)
    seo_audit.authorize_site(db, site, user, body.note)
    return {"id": site.id, "authorized": True}


@router.post("/seo/sites/{sid}/audits", status_code=202)
def audit_start(sid: int, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    try:
        a = seo_audit.start_audit(db, _get(db, SeoSite, sid), user)
    except ValueError as exc:
        raise bad(exc) from exc
    return {"audit_id": a.id, "status": a.status}


@router.get("/seo/audits/{aid}")
def audit_get(aid: int, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    a = _get(db, SeoAudit, aid)
    return {"id": a.id, "status": a.status, "pages_crawled": a.pages_crawled, "summary": a.summary, "error": a.error}


@router.get("/seo/issues")
def issue_list(client_id: int, status: str | None = None, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    q = select(SeoIssue).where(SeoIssue.client_id == client_id)
    if status:
        q = q.where(SeoIssue.status == status)
    return [{k: getattr(i, k) for k in ("id", "code", "finding", "evidence", "url", "severity", "impact", "recommended_fix", "effort",
                                         "responsible_party", "status", "detection", "before_value", "after_value")} for i in db.scalars(q)]


class IssueStatusIn(BaseModel):
    status: str


@router.patch("/seo/issues/{iid}")
def issue_update(iid: int, body: IssueStatusIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    i = _get(db, SeoIssue, iid)
    try:
        seo_audit.update_issue_status(db, i, body.status, user)
    except ValueError as exc:
        raise bad(exc) from exc
    return {"id": i.id, "status": i.status, "after_value": i.after_value}


@router.post("/seo/clients/{cid}/search-metrics")
def search_metrics(cid: int, body: CsvIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    _get(db, Client, cid)
    return import_search_csv(db, cid, body.csv, "search_console_export")


class ReportIn(BaseModel):
    client_id: int
    period_start: date
    period_end: date


@router.post("/client-reports", status_code=201)
def report_create(body: ReportIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    try:
        r = client_reports.generate_report(db, _get(db, Client, body.client_id), body.period_start, body.period_end, user)
    except ValueError as exc:
        raise bad(exc) from exc
    return {"id": r.id, "status": r.status, "data": r.data}


@router.get("/client-reports/{rid}")
def report_get(rid: int, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    r = _get(db, ClientReport, rid)
    return {"id": r.id, "client_id": r.client_id, "status": r.status, "data": r.data, "delivery": r.delivery}


@router.post("/client-reports/{rid}/publish")
def report_publish(rid: int, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    r = _get(db, ClientReport, rid)
    if r.status == "draft":
        r.status = "final"
    return {"id": r.id, "status": r.status}


class SendIn(BaseModel):
    to: EmailStr


@router.post("/client-reports/{rid}/send")
def report_send(rid: int, body: SendIn, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    return {"delivery": client_reports.send_report(db, _get(db, ClientReport, rid), body.to, user)}

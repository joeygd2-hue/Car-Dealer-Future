"""Finance, packages, costs API (admin only — internal business data)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models.base import utcnow
from app.models.costs import Alert, Budget
from app.models.finance import BankTransaction, ClientAgreement, Expense, ExpenseCategory, Invoice, ServicePackage
from app.security.auth import AuthContext, require_admin
from app.services import costs, finance, packages
from app.services.maintenance import store_attachment
from app.web.api_core import bad, client_or_404

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])


def _period(start: date | None, end: date | None) -> tuple[date, date]:
    today = utcnow().date()
    return start or date(today.year, today.month, 1), end or today


# ---------------------------------------------------------------- packages --


def pkg_out(db: Session, p: ServicePackage) -> dict:
    return {
        "id": p.id, "code": p.code, "name": p.name, "description": p.description, "features": p.features,
        "all_features": packages.all_features(db, p), "includes_packages": p.includes_packages,
        "setup_fee_cents": p.setup_fee_cents, "recurring_fee_cents": p.recurring_fee_cents, "billing_frequency": p.billing_frequency,
        "currency": p.currency, "active": p.active, "pricing_status": p.pricing_status,
    }


@router.get("/packages")
def list_packages(db: Session = Depends(get_db)):
    return [pkg_out(db, p) for p in db.scalars(select(ServicePackage).order_by(ServicePackage.id))]


class PackageIn(BaseModel):
    code: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    features: list[str] = []
    includes_packages: list[str] = []
    setup_fee_cents: int = Field(0, ge=0)
    recurring_fee_cents: int = Field(0, ge=0)
    billing_frequency: str = "monthly"


@router.post("/packages", status_code=201)
def create_package(body: PackageIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    if db.scalar(select(ServicePackage.id).where(ServicePackage.code == body.code)):
        raise HTTPException(409, "Code already exists")
    if body.billing_frequency not in ("monthly", "quarterly", "annual", "one_time"):
        raise HTTPException(400, "Invalid billing frequency")
    p = ServicePackage(**body.model_dump(), pricing_status="proposed")
    db.add(p)
    db.commit()
    return pkg_out(db, p)


@router.patch("/packages/{pkg_id}")
def update_package(pkg_id: int, changes: dict, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    p = db.get(ServicePackage, pkg_id)
    if p is None:
        raise HTTPException(404, "Not found")
    try:
        packages.update_package(db, p, changes, ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return pkg_out(db, p)


# -------------------------------------------------------------- agreements --


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
    return {k: getattr(a, k) for k in ("id", "client_id", "package_id", "name", "status", "start_date", "end_date", "setup_fee_cents", "recurring_fee_cents",
                                         "billing_frequency", "discount_percent", "discount_cents", "currency", "cancelled_at", "cancellation_reason")} | {
        "monthly_value_cents": finance.monthly_value_cents(a)}


@router.get("/agreements")
def list_agreements(client_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(ClientAgreement).order_by(ClientAgreement.id.desc())
    if client_id:
        stmt = stmt.where(ClientAgreement.client_id == client_id)
    return [agreement_out(a) for a in db.scalars(stmt)]


@router.post("/agreements", status_code=201)
def create_agreement(body: AgreementIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    c = client_or_404(db, body.client_id)
    pkg = db.get(ServicePackage, body.package_id) if body.package_id else None
    try:
        a = finance.create_agreement(db, client=c, package=pkg, actor=ctx.user, **body.model_dump(exclude={"client_id", "package_id"}))
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return agreement_out(a)


class AgreementActionIn(BaseModel):
    action: str
    effective_date: date | None = None
    reason: str | None = None
    period_start: date | None = None


@router.post("/agreements/{agreement_id}/actions")
def agreement_action(agreement_id: int, body: AgreementActionIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    a = db.get(ClientAgreement, agreement_id)
    if a is None:
        raise HTTPException(404, "Not found")
    try:
        if body.action == "activate":
            finance.activate_agreement(db, a, ctx.user)
        elif body.action == "cancel":
            finance.cancel_agreement(db, a, ctx.user, body.effective_date or utcnow().date(), body.reason or "")
        elif body.action == "invoice":
            inv = finance.invoice_agreement_period(db, a, body.period_start or date(utcnow().year, utcnow().month, 1), ctx.user)
            db.commit()
            return invoice_out(inv)
        else:
            raise ValueError("Unknown action")
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return agreement_out(a)


# ---------------------------------------------------------------- invoices --


def invoice_out(inv: Invoice) -> dict:
    return {
        "id": inv.id, "number": inv.number, "client_id": inv.client_id, "status": inv.status,
        "display_status": finance.invoice_display_status(inv), "issue_date": inv.issue_date, "due_date": inv.due_date,
        "currency": inv.currency, "source": inv.source, "total_cents": finance.invoice_total(inv),
        "paid_cents": finance.invoice_paid(inv), "credited_cents": finance.invoice_credited(inv), "balance_cents": finance.invoice_balance(inv),
        "lines": [{"kind": li.kind, "description": li.description, "quantity": li.quantity, "unit_amount_cents": li.unit_amount_cents, "amount_cents": li.amount_cents} for li in inv.lines],
        "payments": [{"id": p.id, "amount_cents": p.amount_cents, "status": p.status, "received_date": p.received_date, "method": p.method, "source": p.source} for p in inv.payments],
    }


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
def list_invoices(client_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(Invoice).options(selectinload(Invoice.lines), selectinload(Invoice.payments), selectinload(Invoice.credits)).order_by(Invoice.issue_date.desc())
    if client_id:
        stmt = stmt.where(Invoice.client_id == client_id)
    return [invoice_out(i) for i in db.scalars(stmt.limit(1000))]


@router.post("/invoices", status_code=201)
def create_invoice(body: InvoiceIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    c = client_or_404(db, body.client_id)
    try:
        inv = finance.create_invoice(db, client=c, issue_date=body.issue_date, due_date=body.due_date, actor=ctx.user, notes=body.notes,
                                     lines=[finance.LineInput(**li.model_dump()) for li in body.lines])
        if body.issue:
            finance.issue_invoice(db, inv, ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return invoice_out(inv)


def _invoice(db: Session, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Not found")
    return inv


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    return invoice_out(_invoice(db, invoice_id))


class InvoiceActionIn(BaseModel):
    action: str
    reason: str | None = None


@router.post("/invoices/{invoice_id}/actions")
def invoice_action(invoice_id: int, body: InvoiceActionIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    inv = _invoice(db, invoice_id)
    try:
        if body.action == "issue":
            finance.issue_invoice(db, inv, ctx.user)
        elif body.action == "void":
            finance.void_invoice(db, inv, ctx.user, body.reason or "")
        elif body.action == "write_off":
            finance.write_off_invoice(db, inv, ctx.user, body.reason or "")
        else:
            raise ValueError("Unknown action")
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return invoice_out(inv)


class PaymentIn(BaseModel):
    client_id: int
    invoice_id: int | None = None
    amount_cents: int = Field(gt=0)
    received_date: date | None = None
    method: str = "ach"
    status: str = "confirmed"
    reference: str | None = Field(None, max_length=200)
    notes: str | None = None


@router.post("/payments", status_code=201)
def create_payment(body: PaymentIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    client_or_404(db, body.client_id)
    inv = _invoice(db, body.invoice_id) if body.invoice_id else None
    if body.status not in ("confirmed", "pending"):
        raise HTTPException(400, "Manual payments must be confirmed or pending")
    try:
        p = finance.record_payment(db, client_id=body.client_id, invoice=inv, amount_cents=body.amount_cents, received_date=body.received_date,
                                   method=body.method, status=body.status, reference=body.reference, notes=body.notes, actor=ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"id": p.id, "status": p.status, "amount_cents": p.amount_cents, "source": p.source}


class CreditIn(BaseModel):
    client_id: int
    invoice_id: int | None = None
    kind: str
    amount_cents: int = Field(gt=0)
    issued_date: date
    reason: str = Field(min_length=1)


@router.post("/credit-notes", status_code=201)
def create_credit(body: CreditIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    client_or_404(db, body.client_id)
    inv = _invoice(db, body.invoice_id) if body.invoice_id else None
    try:
        c = finance.record_credit_note(db, client_id=body.client_id, invoice=inv, kind=body.kind, amount_cents=body.amount_cents,
                                       issued_date=body.issued_date, reason=body.reason, actor=ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"id": c.id, "kind": c.kind, "amount_cents": c.amount_cents}


# ---------------------------------------------------------------- expenses --


@router.get("/expense-categories")
def list_categories(db: Session = Depends(get_db)):
    return [{"id": c.id, "code": c.code, "name": c.name, "is_acquisition": c.is_acquisition, "active": c.active} for c in db.scalars(select(ExpenseCategory).order_by(ExpenseCategory.name))]


class CategoryIn(BaseModel):
    code: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    name: str = Field(min_length=1, max_length=100)
    is_acquisition: bool = False


@router.post("/expense-categories", status_code=201)
def create_category(body: CategoryIn, db: Session = Depends(get_db)):
    if db.scalar(select(ExpenseCategory.id).where(ExpenseCategory.code == body.code)):
        raise HTTPException(409, "Code exists")
    c = ExpenseCategory(**body.model_dump())
    db.add(c)
    db.commit()
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


def expense_out(e: Expense) -> dict:
    return {k: getattr(e, k) for k in ("id", "expense_date", "vendor", "category_id", "amount_cents", "currency", "business_purpose", "recurrence",
                                         "client_id", "project", "reimbursement_status", "notes", "source", "reconciled")}


@router.get("/expenses")
def list_expenses(start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    s, e = _period(start, end)
    return [expense_out(x) for x in db.scalars(select(Expense).where(Expense.expense_date.between(s, e)).order_by(Expense.expense_date.desc()))]


@router.post("/expenses", status_code=201)
def create_expense(body: ExpenseIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    if body.client_id:
        client_or_404(db, body.client_id)
    try:
        e = finance.create_expense(db, actor=ctx.user, **body.model_dump())
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return expense_out(e)


@router.post("/expenses/{expense_id}/receipt", status_code=201)
async def upload_receipt(expense_id: int, file: UploadFile = File(...), ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    e = db.get(Expense, expense_id)
    if e is None:
        raise HTTPException(404, "Not found")
    data = await file.read()
    try:
        att = store_attachment(db, owner_type="expense", owner_id=e.id, filename=file.filename or "receipt", content_type=file.content_type or "", data=data, actor=ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"id": att.id, "filename": att.filename, "size_bytes": att.size_bytes}


# --------------------------------------------------------------- reporting --


@router.get("/finance/report")
def finance_report(start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    s, e = _period(start, end)
    try:
        return finance.financial_report(db, s, e)
    except ValueError as exc:
        raise bad(exc) from exc


@router.get("/finance/cash-flow")
def cash_flow(start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    today = utcnow().date()
    return finance.cash_flow_trend(db, start or date(today.year - 1, today.month, 1), end or today)


class ScenarioIn(BaseModel):
    fixed_monthly_costs_cents: int | None = Field(None, ge=0)
    price_per_client_cents: int = Field(ge=0)
    variable_cost_per_client_cents: int = Field(0, ge=0)


@router.post("/finance/break-even")
def break_even(body: ScenarioIn, db: Session = Depends(get_db)):
    fixed = body.fixed_monthly_costs_cents if body.fixed_monthly_costs_cents is not None else finance.trailing_fixed_costs(db, utcnow().date())
    return finance.break_even_scenario(fixed_monthly_costs_cents=fixed, price_per_client_cents=body.price_per_client_cents,
                                       variable_cost_per_client_cents=body.variable_cost_per_client_cents)


@router.get("/finance/export/{kind}.csv")
def export(kind: str, start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    s, e = _period(start, end)
    try:
        data = finance.export_csv(db, kind, s, e)
    except ValueError as exc:
        raise bad(exc) from exc
    return Response(data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{kind}-{s}-{e}.csv"'})


@router.post("/finance/bank-import")
def bank_import(body: dict, db: Session = Depends(get_db)):
    res = finance.import_bank_csv(db, str(body.get("csv", ""))[:5_000_000])
    db.commit()
    return res


@router.get("/finance/reconciliation")
def reconciliation(db: Session = Depends(get_db)):
    out = []
    for tx in db.scalars(select(BankTransaction).where(BankTransaction.status == "unmatched").order_by(BankTransaction.posted_date)):
        out.append({"id": tx.id, "date": tx.posted_date, "description": tx.description, "amount_cents": tx.amount_cents, "suggestions": finance.suggest_matches(db, tx)})
    return out


class MatchIn(BaseModel):
    match_type: str
    match_id: int


@router.post("/finance/reconciliation/{tx_id}")
def confirm_match(tx_id: int, body: MatchIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    tx = db.get(BankTransaction, tx_id)
    if tx is None:
        raise HTTPException(404, "Not found")
    try:
        finance.confirm_match(db, tx, body.match_type, body.match_id, ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"id": tx.id, "status": tx.status}


# ------------------------------------------------------------------- costs --


@router.get("/costs/usage")
def usage(start: date | None = None, end: date | None = None, client_id: int | None = None, db: Session = Depends(get_db)):
    s, e = _period(start, end)
    return costs.usage_summary(db, s, e, client_id)


@router.post("/costs/import-billing")
async def import_billing(provider: str = Form(...), service_category: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = (await file.read()).decode("utf-8", "replace")
    res = costs.import_verified_costs_csv(db, content, provider, service_category)
    db.commit()
    return res


class BudgetIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str | None = None
    service_category: str | None = None
    period: str = "monthly"
    amount_micros: int = Field(gt=0)
    alert_threshold_percent: int = Field(80, ge=1, le=100)
    spike_multiplier: int = Field(3, ge=2, le=50)


@router.get("/costs/budgets")
def list_budgets(db: Session = Depends(get_db)):
    return [{k: getattr(b, k) for k in ("id", "name", "provider", "service_category", "period", "amount_micros", "alert_threshold_percent", "spike_multiplier", "active")} for b in db.scalars(select(Budget))]


@router.post("/costs/budgets", status_code=201)
def create_budget(body: BudgetIn, db: Session = Depends(get_db)):
    if body.period not in ("daily", "monthly"):
        raise HTTPException(400, "Invalid period")
    b = Budget(**body.model_dump())
    db.add(b)
    db.commit()
    return {"id": b.id}


@router.post("/costs/check-budgets")
def run_budget_check(db: Session = Depends(get_db)):
    raised = costs.check_budgets(db)
    db.commit()
    return {"alerts": raised}


@router.get("/alerts")
def list_alerts(db: Session = Depends(get_db)):
    return [{"id": a.id, "at": a.created_at, "kind": a.kind, "severity": a.severity, "message": a.message, "acknowledged": a.acknowledged_at is not None}
            for a in db.scalars(select(Alert).order_by(Alert.id.desc()).limit(200))]


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if a is None:
        raise HTTPException(404, "Not found")
    a.acknowledged_at, a.acknowledged_by_user_id = utcnow(), ctx.user.id
    db.commit()
    return {"id": a.id}

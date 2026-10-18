"""Finance operations and reporting (Phase 4, Step 1).

Definitions used throughout (also documented in docs/business/metrics-and-reporting.md):

* Recorded revenue (accrual): sum of invoice lines on *issued* invoices
  (not draft/void) with issue_date in the period, minus `credit` notes issued
  in the period. Written-off invoices remain recorded revenue; the write-off
  is reported separately as bad debt.
* Collected cash: sum of `confirmed` payments with received_date in the
  period, minus `refund` notes issued in the period. Invoices are never cash.
* Outstanding receivables (as of a date): for issued invoices, total minus
  confirmed payments minus credits, floored at zero.
* MRR (contracted): monthly-normalised recurring fee after discounts for
  agreements active on the date. Derived from agreements, not from cash.
"""
from __future__ import annotations

import csv
import hashlib
import io
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db import utcnow
from ..models import (
    MONTHS_PER_PERIOD,
    Client,
    Meeting,
    User,
    BankTransaction,
    ClientAgreement,
    CreditNote,
    Expense,
    ExpenseCategory,
    Invoice,
    InvoiceLine,
    Payment,
)
from .. import audit
from ..sensitive import assert_no_card_data

DEFAULT_EXPENSE_CATEGORIES = [
    ("ai_usage", "AI usage", False),
    ("software", "Software & SaaS", False),
    ("hosting", "Hosting & infrastructure", False),
    ("data_sources", "Data sources & business-data APIs", True),
    ("outreach", "Outreach tools & email delivery", True),
    ("advertising", "Advertising", True),
    ("domains", "Domains & DNS", False),
    ("contractors", "Contractors", False),
    ("telephone", "Telephone & messaging services", False),
    ("payment_processing", "Payment processing fees", False),
    ("other", "Other", False),
]

TAX_DISCLAIMER = (
    "Expense categories are for internal management reporting only and are not tax classifications. "
    "They are not tax, legal or accounting advice; consult a qualified professional."
)


class FinanceError(ValueError):
    pass


def seed_expense_categories(db: Session) -> None:
    for code, name, acq in DEFAULT_EXPENSE_CATEGORIES:
        if not db.scalar(select(ExpenseCategory.id).where(ExpenseCategory.code == code)):
            db.add(ExpenseCategory(code=code, name=name, is_acquisition=acq))
    db.flush()


# ---------------------------------------------------------------- invoices --


def invoice_total(inv: Invoice) -> int:
    return sum(line.amount_cents for line in inv.lines)


def invoice_paid(inv: Invoice) -> int:
    return sum(p.amount_cents for p in inv.payments if p.status == "confirmed")


def invoice_credited(inv: Invoice) -> int:
    return sum(c.amount_cents for c in inv.credits if c.kind == "credit")


def invoice_refunded(inv: Invoice) -> int:
    return sum(c.amount_cents for c in inv.credits if c.kind == "refund")


def invoice_balance(inv: Invoice) -> int:
    if inv.status in ("draft", "void", "written_off"):
        return 0
    return max(0, invoice_total(inv) - invoice_paid(inv) - invoice_credited(inv))


def invoice_display_status(inv: Invoice, today: date | None = None) -> str:
    today = today or utcnow().date()
    if inv.status in ("draft", "void", "written_off"):
        return inv.status
    total, paid = invoice_total(inv), invoice_paid(inv) + invoice_credited(inv)
    if paid >= total:
        return "paid"
    if inv.due_date < today:
        return "overdue"
    return "partially_paid" if paid > 0 else "unpaid"


def next_invoice_number(db: Session, issue: date) -> str:
    prefix = f"INV-{issue.year}{issue.month:02d}-"
    count = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.number.like(prefix + "%"))) or 0
    return f"{prefix}{count + 1:04d}"


@dataclass
class LineInput:
    kind: str
    description: str
    unit_amount_cents: int
    quantity: int = 1
    period_start: date | None = None
    period_end: date | None = None


def create_invoice(
    db: Session,
    *,
    client: Client,
    issue_date: date,
    due_date: date,
    lines: list[LineInput],
    actor: User | None,
    agreement: ClientAgreement | None = None,
    currency: str = "USD",
    notes: str | None = None,
    source: str = "manual",
    external_id: str | None = None,
) -> Invoice:
    if not lines:
        raise FinanceError("An invoice needs at least one line")
    if due_date < issue_date:
        raise FinanceError("Due date cannot be before the issue date")
    assert_no_card_data(notes=notes)
    inv = Invoice(
        client_id=client.id,
        agreement_id=agreement.id if agreement else None,
        number=next_invoice_number(db, issue_date),
        status="draft",
        issue_date=issue_date,
        due_date=due_date,
        currency=currency.upper(),
        notes=notes or "",
        source=source,
        external_id=external_id,
        created_by_id=actor.id if actor else None,
    )
    for li in lines:
        if li.kind not in ("setup", "recurring", "one_time", "discount"):
            raise FinanceError(f"Unknown line kind {li.kind}")
        amt = -abs(li.unit_amount_cents) if li.kind == "discount" else li.unit_amount_cents
        if li.kind != "discount" and amt < 0:
            raise FinanceError("Line amounts must be positive; use a discount line or credit note")
        if li.quantity <= 0:
            raise FinanceError("Quantity must be positive")
        assert_no_card_data(description=li.description)
        inv.lines.append(
            InvoiceLine(
                kind=li.kind,
                description=li.description[:300],
                quantity=li.quantity,
                unit_amount_cents=amt,
                period_start=li.period_start,
                period_end=li.period_end,
            )
        )
    if invoice_total(inv) < 0:
        raise FinanceError("Discounts cannot exceed the invoice total")
    db.add(inv)
    db.flush()
    audit.record(db, "invoice.created", actor=actor, entity=inv, details={"number": inv.number, "total": invoice_total(inv)})
    return inv


def issue_invoice(db: Session, inv: Invoice, actor: User | None) -> None:
    if inv.status != "draft":
        raise FinanceError("Only draft invoices can be issued")
    inv.status, inv.status_changed_at = "issued", utcnow()
    audit.record(db, "invoice.issued", actor=actor, entity=inv)


def void_invoice(db: Session, inv: Invoice, actor: User | None, reason: str) -> None:
    if inv.status not in ("draft", "issued"):
        raise FinanceError("Invoice cannot be voided in its current state")
    if invoice_paid(inv) > 0:
        raise FinanceError("Invoice has confirmed payments; issue a refund/credit instead of voiding")
    inv.status, inv.status_changed_at = "void", utcnow()
    inv.void_reason = reason
    audit.record(db, "invoice.voided", actor=actor, entity=inv, details={"reason": reason})


def write_off_invoice(db: Session, inv: Invoice, actor: User | None, reason: str) -> None:
    if inv.status != "issued":
        raise FinanceError("Only issued invoices can be written off")
    inv.status, inv.status_changed_at = "written_off", utcnow()
    inv.void_reason = reason
    audit.record(db, "invoice.written_off", actor=actor, entity=inv, details={"reason": reason, "balance": invoice_balance(inv)})


PAYMENT_METHODS = ("ach", "wire", "check", "cash", "card_via_processor", "processor_payout", "other")


def record_payment(
    db: Session,
    *,
    client_id: int,
    amount_cents: int,
    received_date: date | None,
    method: str,
    actor: User | None,
    invoice: Invoice | None = None,
    status: str = "confirmed",
    reference: str | None = None,
    notes: str | None = None,
    source: str = "manual",
    external_id: str | None = None,
    currency: str = "USD",
) -> Payment:
    if amount_cents <= 0:
        raise FinanceError("Payment amount must be positive")
    if method not in PAYMENT_METHODS:
        raise FinanceError(f"Unknown payment method {method}")
    if status == "confirmed" and received_date is None:
        raise FinanceError("A confirmed payment requires the date it was received")
    if received_date and received_date > utcnow().date() + timedelta(days=1):
        raise FinanceError("Received date cannot be in the future")
    assert_no_card_data(reference=reference, notes=notes)
    if invoice is not None:
        if invoice.client_id != client_id:
            raise FinanceError("Invoice belongs to a different client")
        if invoice.status != "issued":
            raise FinanceError("Payments can only be applied to issued invoices")
        if status == "confirmed" and amount_cents > invoice_balance(invoice):
            raise FinanceError("Payment exceeds the invoice balance; record the excess as a separate unapplied payment")
        currency = invoice.currency
    if external_id:
        existing = db.scalar(select(Payment).where(Payment.external_id == external_id))
        if existing:
            return existing
    p = Payment(
        client_id=client_id,
        invoice_id=invoice.id if invoice else None,
        amount_cents=amount_cents,
        currency=currency.upper(),
        status=status,
        received_date=received_date,
        method=method,
        reference=reference,
        notes=notes or "",
        source=source,
        external_id=external_id,
    )
    db.add(p)
    db.flush()
    if invoice is not None:
        db.refresh(invoice)
    audit.record(db, "payment.recorded", actor=actor, entity=p, details={"amount": amount_cents, "status": status, "source": source})
    return p


def record_credit_note(
    db: Session,
    *,
    client_id: int,
    kind: str,
    amount_cents: int,
    issued_date: date,
    reason: str,
    actor: User | None,
    invoice: Invoice | None = None,
    source: str = "manual",
    external_id: str | None = None,
) -> CreditNote:
    if kind not in ("refund", "credit"):
        raise FinanceError("kind must be refund or credit")
    if amount_cents <= 0:
        raise FinanceError("Amount must be positive")
    if not reason.strip():
        raise FinanceError("A reason is required")
    assert_no_card_data(reason=reason)
    if invoice is not None:
        if invoice.client_id != client_id:
            raise FinanceError("Invoice belongs to a different client")
        if kind == "refund" and amount_cents > invoice_paid(invoice) - invoice_refunded(invoice):
            raise FinanceError("Refund exceeds confirmed payments on this invoice")
        if kind == "credit" and amount_cents > invoice_balance(invoice):
            raise FinanceError("Credit exceeds the invoice balance")
    if external_id:
        existing = db.scalar(select(CreditNote).where(CreditNote.external_id == external_id))
        if existing:
            return existing
    c = CreditNote(
        client_id=client_id,
        invoice_id=invoice.id if invoice else None,
        kind=kind,
        amount_cents=amount_cents,
        issued_date=issued_date,
        reason=reason,
        source=source,
        external_id=external_id,
        currency=invoice.currency if invoice else "USD",
    )
    db.add(c)
    db.flush()
    audit.record(db, f"credit_note.{kind}", actor=actor, entity=c, details={"amount": amount_cents})
    return c


# -------------------------------------------------------------- agreements --


def monthly_value_cents(a: ClientAgreement) -> int:
    """Recurring fee normalised to one month, after discounts."""
    if a.billing_frequency == "one_time":
        return 0
    per_period = a.recurring_fee_cents * (100 - a.discount_percent) // 100 - a.discount_cents
    per_period = max(0, per_period)
    return per_period // MONTHS_PER_PERIOD[a.billing_frequency]


def agreement_active_on(a: ClientAgreement, d: date) -> bool:
    if a.status not in ("active", "cancelled", "ended", "paused"):
        return False
    if a.start_date > d:
        return False
    if a.status == "paused":
        return False
    if a.cancelled_at and a.cancelled_at <= d:
        return False
    if a.end_date and a.end_date < d:
        return False
    return True


def create_agreement(
    db: Session,
    *,
    client: Client,
    name: str,
    start_date: date,
    actor: User | None,
    package=None,
    setup_fee_cents: int | None = None,
    recurring_fee_cents: int | None = None,
    billing_frequency: str | None = None,
    discount_percent: int = 0,
    discount_cents: int = 0,
    custom_terms: str | None = None,
    end_date: date | None = None,
) -> ClientAgreement:
    if not 0 <= discount_percent <= 100:
        raise FinanceError("Discount percent must be between 0 and 100")
    a = ClientAgreement(
        client_id=client.id,
        package_id=package.id if package else None,
        name=name,
        status="pending",
        start_date=start_date,
        end_date=end_date,
        setup_fee_cents=setup_fee_cents if setup_fee_cents is not None else (package.setup_fee_cents if package else 0),
        recurring_fee_cents=recurring_fee_cents
        if recurring_fee_cents is not None
        else (package.recurring_fee_cents if package else 0),
        billing_frequency=billing_frequency or (package.billing_frequency if package else "monthly"),
        discount_percent=discount_percent,
        discount_cents=discount_cents,
        currency=package.currency if package else "USD",
        features=list(package.features) if package else [],
        custom_terms=custom_terms or "",
    )
    if a.setup_fee_cents < 0 or a.recurring_fee_cents < 0 or discount_cents < 0:
        raise FinanceError("Amounts must be non-negative")
    db.add(a)
    db.flush()
    audit.record(db, "agreement.created", actor=actor, entity=a, details={"name": name})
    return a


def activate_agreement(db: Session, a: ClientAgreement, actor: User | None) -> None:
    if a.status != "pending":
        raise FinanceError("Only pending agreements can be activated")
    a.status = "active"
    audit.record(db, "agreement.activated", actor=actor, entity=a)


def cancel_agreement(db: Session, a: ClientAgreement, actor: User | None, effective: date, reason: str) -> None:
    if a.status not in ("active", "paused", "pending"):
        raise FinanceError("Agreement is not cancellable")
    a.status = "cancelled"
    a.cancelled_at = effective
    a.cancellation_reason = reason
    audit.record(db, "agreement.cancelled", actor=actor, entity=a, details={"effective": effective.isoformat(), "reason": reason})


def invoice_agreement_period(db: Session, a: ClientAgreement, period_start: date, actor: User | None, due_days: int = 15) -> Invoice:
    """Draft an invoice for one billing period of an agreement (setup fee on the first invoice)."""
    if a.status != "active":
        raise FinanceError("Agreement must be active to invoice")
    months = MONTHS_PER_PERIOD.get(a.billing_frequency, 1)
    end_month = period_start.month - 1 + months
    pe_year, pe_month = period_start.year + end_month // 12, end_month % 12 + 1
    period_end = date(pe_year, pe_month, 1) - timedelta(days=1)
    existing = db.scalar(
        select(Invoice)
        .join(InvoiceLine)
        .where(
            Invoice.agreement_id == a.id,
            Invoice.status != "void",
            InvoiceLine.kind == "recurring",
            InvoiceLine.period_start == period_start,
        )
    )
    if existing:
        raise FinanceError(f"Period already invoiced on {existing.number}")
    lines: list[LineInput] = []
    has_prior = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.agreement_id == a.id, Invoice.status != "void"))
    if not has_prior and a.setup_fee_cents:
        lines.append(LineInput("setup", f"{a.name} — setup", a.setup_fee_cents))
    if a.billing_frequency != "one_time" and a.recurring_fee_cents:
        lines.append(LineInput("recurring", f"{a.name} — {period_start:%b %Y}", a.recurring_fee_cents, 1, period_start, period_end))
        disc = a.recurring_fee_cents * a.discount_percent // 100 + a.discount_cents
        if disc:
            lines.append(LineInput("discount", f"Discount ({a.name})", -min(disc, a.recurring_fee_cents)))
    client = db.get(Client, a.client_id)
    return create_invoice(
        db, client=client, issue_date=period_start, due_date=period_start + timedelta(days=due_days),
        lines=lines, actor=actor, agreement=a, currency=a.currency,
    )


def mrr_cents(db: Session, as_of: date, client_id: int | None = None) -> int:
    stmt = select(ClientAgreement).where(ClientAgreement.currency == "USD")
    if client_id:
        stmt = stmt.where(ClientAgreement.client_id == client_id)
    return sum(monthly_value_cents(a) for a in db.scalars(stmt) if agreement_active_on(a, as_of))


def active_client_ids(db: Session, as_of: date) -> set[int]:
    return {a.client_id for a in db.scalars(select(ClientAgreement)) if agreement_active_on(a, as_of) and monthly_value_cents(a) > 0}


# ---------------------------------------------------------------- expenses --


def create_expense(
    db: Session,
    *,
    expense_date: date,
    vendor: str,
    category_id: int,
    amount_cents: int,
    business_purpose: str,
    actor: User | None,
    currency: str = "USD",
    recurrence: str = "one_time",
    client_id: int | None = None,
    project: str | None = None,
    reimbursement_status: str = "not_applicable",
    notes: str | None = None,
    source: str = "manual",
    external_id: str | None = None,
) -> Expense:
    if amount_cents <= 0:
        raise FinanceError("Expense amount must be positive")
    if not vendor.strip() or not business_purpose.strip():
        raise FinanceError("Vendor and business purpose are required")
    if recurrence not in ("one_time", "monthly", "quarterly", "annual"):
        raise FinanceError("Invalid recurrence")
    if reimbursement_status not in ("not_applicable", "pending", "reimbursed", "rejected"):
        raise FinanceError("Invalid reimbursement status")
    cat = db.get(ExpenseCategory, category_id)
    if cat is None or not cat.active:
        raise FinanceError("Unknown or inactive category")
    assert_no_card_data(vendor=vendor, business_purpose=business_purpose, notes=notes, project=project)
    e = Expense(
        expense_date=expense_date,
        vendor=vendor.strip()[:200],
        category_id=category_id,
        amount_cents=amount_cents,
        currency=currency.upper(),
        business_purpose=business_purpose.strip(),
        recurrence=recurrence,
        client_id=client_id,
        project=project,
        reimbursement_status=reimbursement_status,
        notes=notes or "",
        source=source,
        external_id=external_id,
        created_by_id=actor.id if actor else None,
    )
    db.add(e)
    db.flush()
    audit.record(db, "expense.created", actor=actor, entity=e, details={"amount": amount_cents, "vendor": e.vendor})
    return e


# --------------------------------------------------------------- reporting --


def _month_bounds(d: date) -> tuple[date, date]:
    return date(d.year, d.month, 1), date(d.year, d.month, monthrange(d.year, d.month)[1])


def _iter_months(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        yield cur
        cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)


def _ratio(numer: int, denom: int) -> int | None:
    return numer // denom if denom else None


def financial_report(db: Session, start: date, end: date, currency: str = "USD") -> dict:
    """Actuals for a date range. Every figure is derived from recorded data only."""
    if end < start:
        raise FinanceError("End date is before start date")
    invoices = db.scalars(
        select(Invoice).options(selectinload(Invoice.lines), selectinload(Invoice.payments), selectinload(Invoice.credits))
    ).all()
    clients = {c.id: c for c in db.scalars(select(Client))}

    recorded_by_client: dict[int, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    other_currency_invoices = 0
    for inv in invoices:
        if inv.currency != currency:
            other_currency_invoices += 1
            continue
        if inv.status in ("issued", "written_off") and start <= inv.issue_date <= end:
            for line in inv.lines:
                recorded_by_client[inv.client_id] += line.amount_cents
                by_kind[line.kind] += line.amount_cents

    credits = db.scalars(select(CreditNote).where(CreditNote.issued_date.between(start, end), CreditNote.currency == currency)).all()
    credits_total = sum(c.amount_cents for c in credits if c.kind == "credit")
    refunds_total = sum(c.amount_cents for c in credits if c.kind == "refund")
    for c in credits:
        if c.kind == "credit":
            recorded_by_client[c.client_id] -= c.amount_cents

    payments = db.scalars(
        select(Payment).where(Payment.status == "confirmed", Payment.received_date.between(start, end), Payment.currency == currency)
    ).all()
    collected_gross = sum(p.amount_cents for p in payments)
    collected_by_client: dict[int, int] = defaultdict(int)
    for p in payments:
        collected_by_client[p.client_id] += p.amount_cents
    for c in credits:
        if c.kind == "refund":
            collected_by_client[c.client_id] -= c.amount_cents
    pending_payments = db.scalar(
        select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(Payment.status == "pending", Payment.currency == currency)
    )

    # Receivables as of period end, with aging.
    aging = {"current": 0, "1_30": 0, "31_60": 0, "61_90": 0, "over_90": 0}
    outstanding_invoices = []
    for inv in invoices:
        if inv.currency != currency or inv.status != "issued" or inv.issue_date > end:
            continue
        paid = sum(p.amount_cents for p in inv.payments if p.status == "confirmed" and p.received_date and p.received_date <= end)
        cred = sum(c.amount_cents for c in inv.credits if c.kind == "credit" and c.issued_date <= end)
        bal = max(0, invoice_total(inv) - paid - cred)
        if bal <= 0:
            continue
        days = (end - inv.due_date).days
        bucket = "current" if days <= 0 else "1_30" if days <= 30 else "31_60" if days <= 60 else "61_90" if days <= 90 else "over_90"
        aging[bucket] += bal
        outstanding_invoices.append(
            {"number": inv.number, "client": clients[inv.client_id].name, "due_date": inv.due_date.isoformat(), "balance_cents": bal, "days_overdue": max(0, days)}
        )
    written_off = sum(
        invoice_balance_at_writeoff(inv) for inv in invoices if inv.status == "written_off" and inv.status_changed_at and start <= inv.status_changed_at.date() <= end
    )

    expenses = db.scalars(
        select(Expense).options(selectinload(Expense.category)).where(Expense.expense_date.between(start, end), Expense.currency == currency)
    ).all()
    expenses_total = sum(e.amount_cents for e in expenses)
    by_category: dict[str, int] = defaultdict(int)
    acquisition = 0
    client_direct_costs: dict[int, int] = defaultdict(int)
    for e in expenses:
        by_category[e.category.name] += e.amount_cents
        if e.category.is_acquisition:
            acquisition += e.amount_cents
        if e.client_id:
            client_direct_costs[e.client_id] += e.amount_cents
    allocated = sum(client_direct_costs.values())

    # Usage costs (AI etc.) allocated to clients — estimates are labelled separately.
    from .usage import summary as usage_summary

    usage_by_client: dict[int, dict] = {}
    for cid in clients:
        s = usage_summary(db, start, end, client_id=cid)["totals"]
        if s["verified_billing"] or s["estimated"] or s["recorded"]:
            usage_by_client[cid] = s

    recorded_revenue = sum(by_kind.values()) - credits_total
    collected_net = collected_gross - refunds_total

    # Acquisition funnel (our own sales pipeline).
    start_dt = _dt(start)
    end_dt = _dt(end + timedelta(days=1))
    from .reports import stage_entries

    qualified_leads = stage_entries(db, "qualified", start_dt, end_dt, simulated=False)
    meetings_booked = db.scalar(select(func.count()).select_from(Meeting).where(
        Meeting.created_at >= start_dt, Meeting.created_at < end_dt, Meeting.simulated.is_(False),
        Meeting.status.in_(("booked", "rescheduled", "completed", "no_show")))) or 0
    first_agreement: dict[int, date] = {}
    for a in db.scalars(select(ClientAgreement).where(ClientAgreement.status != "pending")):
        if a.client_id not in first_agreement or a.start_date < first_agreement[a.client_id]:
            first_agreement[a.client_id] = a.start_date
    new_clients = [cid for cid, d in first_agreement.items() if start <= d <= end]

    # Retention / churn
    active_start = active_client_ids(db, start)
    active_end = active_client_ids(db, end)
    churned = active_start - active_end
    churn_rate = (len(churned) * 10000 // len(active_start)) / 100 if active_start else None

    billed_clients = [cid for cid, v in recorded_by_client.items() if v > 0]

    per_client = []
    for cid in sorted(set(recorded_by_client) | set(collected_by_client) | set(client_direct_costs) | set(usage_by_client)):
        rev = recorded_by_client.get(cid, 0)
        direct = client_direct_costs.get(cid, 0)
        usage = usage_by_client.get(cid, {})
        usage_cents_est = int(round((usage.get("estimated", 0) + usage.get("verified_billing", 0) + usage.get("recorded", 0)) * 100))
        cost = direct + usage_cents_est
        per_client.append(
            {
                "client_id": cid,
                "client": clients[cid].name if cid in clients else f"#{cid}",
                "recorded_revenue_cents": rev,
                "collected_cents": collected_by_client.get(cid, 0),
                "direct_expense_cents": direct,
                "usage_cost_cents": usage_cents_est,
                "usage_cost_includes_estimates": bool(usage.get("estimated")),
                "gross_margin_percent": round((rev - cost) * 100 / rev, 1) if rev > 0 and cost > 0 else None,
            }
        )

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "currency": currency},
        "basis": "actuals",
        "recorded_revenue_cents": recorded_revenue,
        "revenue_by_kind_cents": dict(by_kind),
        "credits_cents": credits_total,
        "refunds_cents": refunds_total,
        "collected_gross_cents": collected_gross,
        "collected_net_cents": collected_net,
        "pending_payments_cents": int(pending_payments or 0),
        "outstanding_receivables_cents": sum(aging.values()),
        "receivables_aging_cents": aging,
        "outstanding_invoices": sorted(outstanding_invoices, key=lambda r: -r["days_overdue"]),
        "written_off_cents": written_off,
        "expenses_cents": expenses_total,
        "expenses_by_category_cents": dict(by_category),
        "operating_result_cash_cents": collected_net - expenses_total,
        "operating_result_accrual_cents": recorded_revenue - expenses_total,
        "mrr_cents": mrr_cents(db, end),
        "arr_cents": mrr_cents(db, end) * 12,
        "active_clients": len(active_end),
        "avg_revenue_per_billed_client_cents": _ratio(recorded_revenue, len(billed_clients)),
        "avg_mrr_per_active_client_cents": _ratio(mrr_cents(db, end), len(active_end)),
        "acquisition_spend_cents": acquisition,
        "new_clients": len(new_clients),
        "qualified_leads": qualified_leads,
        "meetings_booked": meetings_booked,
        "cost_per_acquired_client_cents": _ratio(acquisition, len(new_clients)),
        "cost_per_qualified_lead_cents": _ratio(acquisition, qualified_leads),
        "cost_per_booked_meeting_cents": _ratio(acquisition, meetings_booked),
        "clients_active_at_start": len(active_start),
        "clients_churned": len(churned),
        "churn_rate_percent": churn_rate,
        "retention_rate_percent": round(100 - churn_rate, 2) if churn_rate is not None else None,
        "per_client": per_client,
        "cost_allocation": {
            "allocated_expense_cents": allocated,
            "unallocated_expense_cents": expenses_total - allocated,
            "note": "Gross margin is shown only for clients with directly allocated costs; unallocated overhead is excluded.",
        },
        "excluded_other_currency_invoices": other_currency_invoices,
        "tax_disclaimer": TAX_DISCLAIMER,
    }


def invoice_balance_at_writeoff(inv: Invoice) -> int:
    return max(0, invoice_total(inv) - invoice_paid(inv) - invoice_credited(inv))


def _dt(d: date):
    from datetime import datetime

    return datetime(d.year, d.month, d.day)


def cash_flow_trend(db: Session, start: date, end: date, forecast_months: int = 3) -> dict:
    """Monthly actual collections vs expenses, plus a clearly labelled forecast."""
    months = []
    for m in _iter_months(start, end):
        ms, me = _month_bounds(m)
        collected = db.scalar(
            select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
                Payment.status == "confirmed", Payment.received_date.between(ms, me), Payment.currency == "USD"
            )
        )
        refunds = db.scalar(
            select(func.coalesce(func.sum(CreditNote.amount_cents), 0)).where(
                CreditNote.kind == "refund", CreditNote.issued_date.between(ms, me)
            )
        )
        spent = db.scalar(
            select(func.coalesce(func.sum(Expense.amount_cents), 0)).where(Expense.expense_date.between(ms, me), Expense.currency == "USD")
        )
        net_in = int(collected) - int(refunds)
        months.append({"month": ms.strftime("%Y-%m"), "basis": "actual", "cash_in_cents": net_in, "cash_out_cents": int(spent), "net_cents": net_in - int(spent)})
    forecast = []
    # Forecast: contracted MRR as expected inflow; the last actual month's monthly-recurring
    # expenses as expected outflow.
    last_ms, last_me = _month_bounds(end)
    recent_recurring = db.scalar(
        select(func.coalesce(func.sum(Expense.amount_cents), 0)).where(
            Expense.recurrence == "monthly", Expense.expense_date.between(last_ms, last_me)
        )
    )
    recurring_monthly = int(recent_recurring or 0)
    nxt = date(end.year + (end.month == 12), end.month % 12 + 1, 1)
    for _ in range(forecast_months):
        expected_in = mrr_cents(db, nxt)
        forecast.append(
            {"month": nxt.strftime("%Y-%m"), "basis": "forecast", "cash_in_cents": expected_in, "cash_out_cents": recurring_monthly, "net_cents": expected_in - recurring_monthly}
        )
        nxt = date(nxt.year + (nxt.month == 12), nxt.month % 12 + 1, 1)
    return {
        "actuals": months,
        "forecast": forecast,
        "forecast_assumptions": [
            "Inflow = contracted MRR from active agreements (assumes every invoice is paid on time).",
            "Outflow = monthly recurring expenses recorded in the last actual month; excludes one-time costs.",
            "This is a projection, not a measurement.",
        ],
    }


def break_even_scenario(
    *, fixed_monthly_costs_cents: int, price_per_client_cents: int, variable_cost_per_client_cents: int
) -> dict:
    """Hypothetical scenario. Inputs are assumptions, not recorded data."""
    contribution = price_per_client_cents - variable_cost_per_client_cents
    clients_needed = None if contribution <= 0 else -(-fixed_monthly_costs_cents // contribution)
    return {
        "basis": "hypothetical_scenario",
        "inputs": {
            "fixed_monthly_costs_cents": fixed_monthly_costs_cents,
            "price_per_client_cents": price_per_client_cents,
            "variable_cost_per_client_cents": variable_cost_per_client_cents,
        },
        "contribution_margin_per_client_cents": contribution,
        "clients_needed_to_break_even": clients_needed,
        "note": "Hypothetical. Not a forecast and not based on validated market pricing."
        if clients_needed is not None
        else "Price does not exceed variable cost per client; break-even is impossible under these assumptions.",
    }


def trailing_fixed_costs(db: Session, as_of: date, months: int = 3) -> int:
    """Average monthly unallocated expenses over the trailing window (a suggested scenario input)."""
    start = as_of - timedelta(days=30 * months)
    total = db.scalar(
        select(func.coalesce(func.sum(Expense.amount_cents), 0)).where(
            Expense.expense_date.between(start, as_of), Expense.client_id.is_(None)
        )
    )
    return int(total or 0) // months


# ---------------------------------------------------------------- exports ---


def _cents(c: int | None) -> str:
    return "" if c is None else f"{c / 100:.2f}"


def export_csv(db: Session, kind: str, start: date, end: date) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    clients = {c.id: c.name for c in db.scalars(select(Client))}
    if kind == "invoices":
        w.writerow(["number", "client", "status", "display_status", "issue_date", "due_date", "currency", "total", "paid", "credited", "balance", "source"])
        stmt = select(Invoice).options(selectinload(Invoice.lines), selectinload(Invoice.payments), selectinload(Invoice.credits)).where(Invoice.issue_date.between(start, end)).order_by(Invoice.issue_date)
        for inv in db.scalars(stmt):
            w.writerow([inv.number, clients.get(inv.client_id), inv.status, invoice_display_status(inv), inv.issue_date, inv.due_date, inv.currency,
                        _cents(invoice_total(inv)), _cents(invoice_paid(inv)), _cents(invoice_credited(inv)), _cents(invoice_balance(inv)), inv.source])
    elif kind == "payments":
        w.writerow(["id", "client", "invoice", "status", "received_date", "amount", "currency", "method", "reference", "source", "reconciled"])
        stmt = select(Payment).options(selectinload(Payment.invoice)).where(Payment.received_date.between(start, end)).order_by(Payment.received_date)
        for p in db.scalars(stmt):
            w.writerow([p.id, clients.get(p.client_id), p.invoice.number if p.invoice else "", p.status, p.received_date, _cents(p.amount_cents), p.currency, p.method, p.reference or "", p.source, p.reconciled])
    elif kind == "expenses":
        w.writerow(["id", "date", "vendor", "category", "amount", "currency", "recurrence", "business_purpose", "client", "project", "reimbursement", "source", "reconciled"])
        stmt = select(Expense).options(selectinload(Expense.category)).where(Expense.expense_date.between(start, end)).order_by(Expense.expense_date)
        for e in db.scalars(stmt):
            w.writerow([e.id, e.expense_date, e.vendor, e.category.name, _cents(e.amount_cents), e.currency, e.recurrence, e.business_purpose, clients.get(e.client_id, ""), e.project or "", e.reimbursement_status, e.source, e.reconciled])
    elif kind == "summary":
        r = financial_report(db, start, end)
        w.writerow(["metric", "value", "basis"])
        for key in ("recorded_revenue_cents", "collected_net_cents", "outstanding_receivables_cents", "expenses_cents", "operating_result_cash_cents", "operating_result_accrual_cents", "mrr_cents", "arr_cents"):
            w.writerow([key.replace("_cents", ""), _cents(r[key]), "actual" if "rr" not in key else "contracted"])
    else:
        raise FinanceError(f"Unknown export {kind}")
    return out.getvalue()


# ----------------------------------------------------------- reconciliation --


def import_bank_csv(db: Session, content: str) -> dict:
    """Import bank statement lines: columns date, description, amount (+in / -out)."""
    batch = hashlib.sha256(content.encode()).hexdigest()[:16]
    reader = csv.DictReader(io.StringIO(content))
    created, dupes, errors = 0, 0, []
    # Identical lines can legitimately occur (two equal charges on one day), so the
    # fingerprint includes the occurrence number of that (date, description, amount).
    seen: dict[tuple, int] = defaultdict(int)
    for i, row in enumerate(reader, start=2):
        try:
            d = date.fromisoformat(row["date"].strip())
            desc = row["description"].strip()[:300]
            amount = int(round(float(row["amount"].replace(",", "").replace("$", "")) * 100))
        except (KeyError, ValueError, AttributeError) as exc:
            errors.append(f"line {i}: {exc}")
            continue
        if amount == 0:
            continue
        seen[(d, desc, amount)] += 1
        fp = hashlib.sha256(f"{d}|{desc}|{amount}|{seen[(d, desc, amount)]}".encode()).hexdigest()
        if db.scalar(select(BankTransaction.id).where(BankTransaction.fingerprint == fp)):
            dupes += 1
            continue
        db.add(BankTransaction(posted_date=d, description=desc, amount_cents=amount, import_batch=batch, fingerprint=fp))
        created += 1
    db.flush()
    return {"created": created, "duplicates": dupes, "errors": errors, "batch": batch}


def suggest_matches(db: Session, tx: BankTransaction, window_days: int = 5) -> list[dict]:
    lo, hi = tx.posted_date - timedelta(days=window_days), tx.posted_date + timedelta(days=window_days)
    out = []
    if tx.amount_cents > 0:
        for p in db.scalars(select(Payment).where(Payment.amount_cents == tx.amount_cents, Payment.reconciled.is_(False), Payment.status.in_(("confirmed", "pending")))):
            if p.received_date is None or lo <= p.received_date <= hi:
                out.append({"type": "payment", "id": p.id, "date": str(p.received_date), "amount_cents": p.amount_cents, "label": p.reference or f"Payment #{p.id}"})
    else:
        for e in db.scalars(select(Expense).where(Expense.amount_cents == -tx.amount_cents, Expense.reconciled.is_(False), Expense.expense_date.between(lo, hi))):
            out.append({"type": "expense", "id": e.id, "date": str(e.expense_date), "amount_cents": e.amount_cents, "label": e.vendor})
        for c in db.scalars(select(CreditNote).where(CreditNote.kind == "refund", CreditNote.amount_cents == -tx.amount_cents, CreditNote.issued_date.between(lo, hi))):
            out.append({"type": "refund", "id": c.id, "date": str(c.issued_date), "amount_cents": c.amount_cents, "label": c.reason[:60]})
    return out


def confirm_match(db: Session, tx: BankTransaction, match_type: str, match_id: int, actor: User) -> None:
    if tx.status != "unmatched":
        raise FinanceError("Transaction already reconciled")
    now = utcnow()
    if match_type == "payment":
        p = db.get(Payment, match_id)
        if p is None or p.amount_cents != tx.amount_cents:
            raise FinanceError("Payment not found or amount mismatch")
        p.reconciled, p.reconciled_at, p.reconciled_by_id = True, now, actor.id
        if p.status == "pending":
            p.status = "confirmed"
            p.received_date = tx.posted_date
        tx.matched_payment_id = p.id
    elif match_type == "expense":
        e = db.get(Expense, match_id)
        if e is None or e.amount_cents != -tx.amount_cents:
            raise FinanceError("Expense not found or amount mismatch")
        e.reconciled = True
        tx.matched_expense_id = e.id
    elif match_type == "refund":
        c = db.get(CreditNote, match_id)
        if c is None or c.amount_cents != -tx.amount_cents:
            raise FinanceError("Refund not found or amount mismatch")
        tx.matched_credit_id = c.id
    else:
        raise FinanceError("Unknown match type")
    tx.status = "matched"
    audit.record(db, "reconciliation.matched", actor=actor, entity=tx, details={"type": match_type, "id": match_id})

"""Finance (managers view, admins change), service catalog and usage costs."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db, utcnow
from ..models import Alert, Attachment, BankTransaction, Budget, Client, Expense, ExpenseCategory, Invoice, ServicePackage, User
from ..services import finance, packages, usage
from ..services.maintenance import read_attachment, store_attachment
from .deps import check_csrf, flash, render, require
from .forms import parse_date, parse_money

router = APIRouter(dependencies=[Depends(check_csrf)])


def period(start: str | None, end: str | None) -> tuple[date, date]:
    today = utcnow().date()
    return (parse_date(start) if start else date(today.year, today.month, 1)), (parse_date(end) if end else today)


def months_back(d: date, n: int) -> date:
    idx = d.year * 12 + d.month - 1 - n
    return date(idx // 12, idx % 12 + 1, 1)


def _overview(request: Request, user: User, db: Session, start: str | None, end: str | None, scenario=None):
    try:
        s, e = period(start, end)
        report = finance.financial_report(db, s, e)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/finance", status_code=303)
    return render(request, "finance.html", user, r=report, start=s, end=e, flow=finance.cash_flow_trend(db, months_back(e, 5), e),
                  suggested_fixed=finance.trailing_fixed_costs(db, e), scenario=scenario)


@router.get("/finance")
def overview(request: Request, start: str | None = None, end: str | None = None, user: User = Depends(require("manager")),
             db: Session = Depends(get_db)):
    return _overview(request, user, db, start, end)


@router.post("/finance/break-even")
def break_even(request: Request, fixed: str = Form(...), price: str = Form(...), variable: str = Form("0"),
               user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    try:
        scen = finance.break_even_scenario(fixed_monthly_costs_cents=parse_money(fixed), price_per_client_cents=parse_money(price),
                                           variable_cost_per_client_cents=parse_money(variable))
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/finance", status_code=303)
    return _overview(request, user, db, None, None, scenario=scen)


@router.get("/finance/export/{kind}.csv")
def export(kind: str, start: str | None = None, end: str | None = None, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    s, e = period(start, end)
    try:
        data = finance.export_csv(db, kind, s, e)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{kind}-{s}-{e}.csv"'})


@router.get("/finance/invoices")
def invoices(request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Invoice).options(selectinload(Invoice.lines), selectinload(Invoice.payments), selectinload(Invoice.credits))
                      .order_by(Invoice.issue_date.desc()).limit(500)).all()
    return render(request, "invoices.html", user, invoices=rows, names={c.id: c.name for c in db.scalars(select(Client).order_by(Client.name))},
                  f=finance, today=utcnow().date())


@router.post("/finance/invoices")
def invoice_create(request: Request, client_id: int = Form(...), issue_date: str = Form(...), due_date: str = Form(...), kind: str = Form("one_time"),
                   description: str = Form(...), amount: str = Form(...), discount: str = Form(""), notes: str = Form(""),
                   user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    c = db.get(Client, client_id)
    try:
        if c is None:
            raise ValueError("Unknown client")
        lines = [finance.LineInput(kind, description, parse_money(amount))]
        if discount:
            lines.append(finance.LineInput("discount", "Discount", -parse_money(discount)))
        inv = finance.create_invoice(db, client=c, issue_date=parse_date(issue_date), due_date=parse_date(due_date), lines=lines, actor=user, notes=notes)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/finance/invoices", status_code=303)
    flash(request, "Draft invoice created (manual entry). It is not revenue until issued, and not cash until paid.")
    return RedirectResponse(f"/finance/invoices/{inv.id}", status_code=303)


@router.get("/finance/invoices/{iid}")
def invoice_detail(iid: int, request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    inv = db.get(Invoice, iid)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    return render(request, "invoice_detail.html", user, inv=inv, client=db.get(Client, inv.client_id), f=finance,
                  methods=finance.PAYMENT_METHODS, today=utcnow().date())


@router.post("/finance/invoices/{iid}/{action}")
def invoice_action(iid: int, action: str, request: Request, reason: str = Form(""), amount: str = Form(""), received_date: str = Form(""),
                   method: str = Form("ach"), reference: str = Form(""), kind: str = Form("credit"), user: User = Depends(require("admin")),
                   db: Session = Depends(get_db)):
    inv = db.get(Invoice, iid)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    try:
        if action == "issue":
            finance.issue_invoice(db, inv, user)
        elif action == "void":
            finance.void_invoice(db, inv, user, reason)
        elif action == "write_off":
            finance.write_off_invoice(db, inv, user, reason)
        elif action == "payment":
            finance.record_payment(db, client_id=inv.client_id, invoice=inv, amount_cents=parse_money(amount),
                                   received_date=parse_date(received_date, "received date"), method=method, reference=reference or None, actor=user)
        elif action == "credit":
            finance.record_credit_note(db, client_id=inv.client_id, invoice=inv, kind=kind, amount_cents=parse_money(amount),
                                       issued_date=utcnow().date(), reason=reason, actor=user)
        else:
            raise ValueError("Unknown action")
        flash(request, "Saved.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/finance/invoices/{iid}", status_code=303)


@router.get("/finance/expenses")
def expenses(request: Request, start: str | None = None, end: str | None = None, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    s, e = period(start, end)
    rows = db.scalars(select(Expense).options(selectinload(Expense.category)).where(Expense.expense_date.between(s, e))
                      .order_by(Expense.expense_date.desc())).all()
    receipts = {a.owner_id: a for a in db.scalars(select(Attachment).where(Attachment.owner_type == "expense"))}
    return render(request, "expenses.html", user, expenses=rows, receipts=receipts, start=s, end=e, disclaimer=finance.TAX_DISCLAIMER,
                  categories=db.scalars(select(ExpenseCategory).where(ExpenseCategory.active.is_(True)).order_by(ExpenseCategory.name)).all(),
                  clients=db.scalars(select(Client).order_by(Client.name)).all(), today=utcnow().date())


@router.post("/finance/expenses")
async def expense_create(request: Request, expense_date: str = Form(...), vendor: str = Form(...), category_id: int = Form(...),
                         amount: str = Form(...), currency: str = Form("USD"), business_purpose: str = Form(...),
                         recurrence: str = Form("one_time"), client_id: str = Form(""), project: str = Form(""),
                         reimbursement_status: str = Form("not_applicable"), notes: str = Form(""), receipt: UploadFile | None = File(None),
                         user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    try:
        with db.begin_nested():
            e = finance.create_expense(db, expense_date=parse_date(expense_date), vendor=vendor, category_id=category_id,
                                       amount_cents=parse_money(amount), currency=currency.strip().upper()[:3] or "USD",
                                       business_purpose=business_purpose, recurrence=recurrence, client_id=int(client_id) if client_id else None,
                                       project=project or None, reimbursement_status=reimbursement_status, notes=notes, actor=user)
            if receipt is not None and receipt.filename:
                store_attachment(db, owner_type="expense", owner_id=e.id, filename=receipt.filename,
                                 content_type=receipt.content_type or "", data=await receipt.read(), actor=user)
        flash(request, "Expense recorded (manual entry).")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/finance/expenses", status_code=303)


@router.get("/finance/attachments/{att_id}")
def attachment(att_id: int, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    att = db.get(Attachment, att_id)
    if att is None:
        raise HTTPException(404, "Attachment not found")
    from urllib.parse import quote

    return Response(read_attachment(att), media_type=att.content_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(att.filename)}"})


@router.get("/finance/reconciliation")
def reconciliation(request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    txs = db.scalars(select(BankTransaction).where(BankTransaction.status == "unmatched").order_by(BankTransaction.posted_date)).all()
    return render(request, "reconciliation.html", user, txs=[(t, finance.suggest_matches(db, t)) for t in txs])


@router.post("/finance/reconciliation/import")
async def reconciliation_import(request: Request, file: UploadFile = File(...), user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    res = finance.import_bank_csv(db, (await file.read()).decode("utf-8", "replace")[:5_000_000])
    flash(request, f"Imported {res['created']} lines; {res['duplicates']} duplicates skipped; {len(res['errors'])} errors.",
          "warn" if res["errors"] else "info")
    return RedirectResponse("/finance/reconciliation", status_code=303)


@router.post("/finance/reconciliation/{tx_id}")
def reconciliation_match(tx_id: int, request: Request, match: str = Form(...), user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    tx = db.get(BankTransaction, tx_id)
    try:
        if tx is None:
            raise ValueError("Transaction not found")
        mtype, mid = match.split(":")
        finance.confirm_match(db, tx, mtype, int(mid), user)
        flash(request, "Matched.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/finance/reconciliation", status_code=303)


@router.get("/packages")
def package_list(request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    rows = db.scalars(select(ServicePackage).order_by(ServicePackage.id)).all()
    return render(request, "packages.html", user, packages=rows, all_features=lambda p: packages.all_features(db, p))


@router.post("/packages/{pid}")
def package_update(pid: int, request: Request, name: str = Form(...), setup_fee: str = Form("0"), recurring_fee: str = Form("0"),
                   billing_frequency: str = Form("monthly"), pricing_status: str = Form("proposed"), features: str = Form(""), active: str = Form(""),
                   user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    p = db.get(ServicePackage, pid)
    if p is None:
        raise HTTPException(404, "Package not found")
    try:
        packages.update_package(db, p, {"name": name, "setup_fee_cents": parse_money(setup_fee), "recurring_fee_cents": parse_money(recurring_fee),
                                        "billing_frequency": billing_frequency, "pricing_status": pricing_status,
                                        "features": [x.strip() for x in features.splitlines() if x.strip()], "active": active == "1"}, user)
        flash(request, "Package saved.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/packages", status_code=303)


@router.get("/costs")
def costs_page(request: Request, start: str | None = None, end: str | None = None, user: User = Depends(require("manager")),
               db: Session = Depends(get_db)):
    s, e = period(start, end)
    return render(request, "costs.html", user, use=usage.summary(db, s, e), start=s, end=e, budgets=db.scalars(select(Budget)).all(),
                  alerts=db.scalars(select(Alert).order_by(Alert.id.desc()).limit(50)).all(), categories=usage.CATEGORIES)


@router.post("/costs/budgets")
def budget_create(request: Request, name: str = Form(...), category: str = Form(""), budget_period: str = Form("monthly"), amount: str = Form(...),
                  threshold: int = Form(80), user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    try:
        if budget_period not in ("daily", "monthly") or not 1 <= threshold <= 100:
            raise ValueError("Invalid period or threshold")
        db.add(Budget(name=name.strip()[:100], category=category or None, period=budget_period, amount_cents=parse_money(amount),
                      alert_threshold_percent=threshold))
        flash(request, "Budget saved. Checked hourly by the worker.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/costs", status_code=303)


@router.post("/costs/import-billing")
async def import_billing(request: Request, provider: str = Form(...), category: str = Form(...), file: UploadFile = File(...),
                         user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    res = usage.import_billing_csv(db, (await file.read()).decode("utf-8", "replace")[:5_000_000], provider.strip(), category)
    flash(request, f"Imported {res['created']} verified billing rows; {len(res['errors'])} errors.", "warn" if res["errors"] else "info")
    return RedirectResponse("/costs", status_code=303)


@router.post("/alerts/{aid}/ack")
def alert_ack(aid: int, request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    a = db.get(Alert, aid)
    if a and not a.acknowledged_at:
        a.acknowledged_at, a.acknowledged_by_id = utcnow(), user.id
    return RedirectResponse("/costs", status_code=303)

"""Usage and cost tracking for AI and other integrations (Phase 4, step 2).

Builds on Phase 1's ``cost_entries`` ledger (also used by the agent's budget checks), adding
provider/metric/quantity/client and a cost basis:

* verified_billing — amount copied from the provider's own billing export (``verified=True``)
* estimated        — measured quantity x the per-unit rate in the ``cost_estimates`` setting
* recorded         — an actual amount entered by a person (not from provider billing)
* unknown          — usage measured but no rate on file; the cost is not claimed (amount 0)

Never present estimated or unknown costs as exact.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Budget, CostEntry
from . import alerts
from .settings_store import get_setting

CATEGORIES = ("ai", "email", "data_api", "messaging", "hosting", "calendar", "video", "seo", "other")


def rate_for(db: Session, provider: str, metric: str) -> float | None:
    rates = get_setting(db, "cost_estimates") or {}
    value = rates.get(f"{provider}:{metric}")
    return None if value is None else float(value)


def record_usage(db: Session, *, provider: str, category: str, metric: str, quantity: float, ref: str | None = None,
                 client_id: int | None = None, verified_amount: float | None = None, note: str = "",
                 incurred_on: datetime | None = None) -> CostEntry:
    """Record measured usage once per ``ref`` (idempotent). Returns the (possibly existing) row."""
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    if ref:
        existing = db.scalar(select(CostEntry).where(CostEntry.ref == ref))
        if existing:
            return existing
    if verified_amount is not None:
        amount, estimate, verified = verified_amount, False, True
    else:
        rate = rate_for(db, provider, metric)
        amount, estimate, verified = (0.0, False, False) if rate is None else (quantity * rate, True, False)
        if rate is None:
            note = (note + " no rate on file; cost not claimed").strip()
    row = CostEntry(incurred_on=incurred_on or utcnow(), category=category, amount=round(amount, 6), is_estimate=estimate,
                    verified=verified, provider=provider, metric=metric, quantity=quantity, client_id=client_id,
                    note=note[:500], ref=ref)
    db.add(row)
    db.flush()
    return row


def import_billing_csv(db: Session, content: str, provider: str, category: str) -> dict:
    """Import a provider billing export with columns date, metric, quantity, cost (currency units)."""
    created, errors = 0, []
    for i, row in enumerate(csv.DictReader(io.StringIO(content)), start=2):
        try:
            d = date.fromisoformat(row["date"].strip())
            metric = row["metric"].strip()
            ref = f"billing:{provider}:{d.isoformat()}:{metric}"
            if db.scalar(select(CostEntry.id).where(CostEntry.ref == ref)):
                continue
            record_usage(db, provider=provider, category=category, metric=metric, quantity=float(row.get("quantity") or 0),
                         verified_amount=float(row["cost"]), ref=ref, note="provider billing import",
                         incurred_on=datetime(d.year, d.month, d.day))
            created += 1
        except (KeyError, ValueError) as exc:
            errors.append(f"line {i}: {exc}")
    return {"created": created, "errors": errors}


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return datetime(start.year, start.month, start.day), datetime(end.year, end.month, end.day) + timedelta(days=1)


def summary(db: Session, start: date, end: date, client_id: int | None = None) -> dict:
    """Totals per provider/metric/basis. Verified, estimated and recorded amounts are never merged."""
    s, e = _day_bounds(start, end)
    stmt = select(CostEntry).where(CostEntry.incurred_on >= s, CostEntry.incurred_on < e)
    if client_id is not None:
        stmt = stmt.where(CostEntry.client_id == client_id)
    groups: dict[tuple, dict] = {}
    totals = {"verified_billing": 0.0, "estimated": 0.0, "recorded": 0.0, "unknown_records": 0}
    for row in db.scalars(stmt):
        key = (row.provider or row.category, row.category, row.metric or "", row.basis)
        g = groups.setdefault(key, {"provider": key[0], "category": key[1], "metric": key[2], "basis": key[3],
                                    "quantity": 0.0, "amount": 0.0, "records": 0})
        g["quantity"] += float(row.quantity or 0)
        g["amount"] += float(row.amount or 0)
        g["records"] += 1
        if row.basis == "unknown":
            totals["unknown_records"] += 1
        else:
            totals[row.basis] += float(row.amount or 0)
    rows = sorted(groups.values(), key=lambda g: (g["provider"], g["metric"]))
    for g in rows:
        g["amount"] = round(g["amount"], 4)
    return {"rows": rows, "totals": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in totals.items()}}


def spent(db: Session, start: datetime, end: datetime, category: str | None = None) -> float:
    stmt = select(func.coalesce(func.sum(CostEntry.amount), 0)).where(CostEntry.incurred_on >= start, CostEntry.incurred_on < end)
    if category:
        stmt = stmt.where(CostEntry.category == category)
    return float(db.scalar(stmt) or 0)


def check_budgets(db: Session, now: datetime | None = None) -> list[str]:
    """Raise (deduplicated) alerts for budgets crossing their threshold or limit, and for usage spikes."""
    now = now or utcnow()
    today = datetime(now.year, now.month, now.day)
    raised = []
    for b in db.scalars(select(Budget).where(Budget.active.is_(True))):
        if b.period == "daily":
            start, end, slot = today, today + timedelta(days=1), today.date().isoformat()
        else:
            start = datetime(now.year, now.month, 1)
            end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
            slot = f"{now.year}-{now.month:02d}"
        used_cents = int(round(spent(db, start, end, b.category) * 100))
        pct = used_cents * 100 // b.amount_cents if b.amount_cents else 0
        label = f"${used_cents / 100:,.2f} of ${b.amount_cents / 100:,.2f} ({b.period}; includes estimates)"
        if pct >= 100:
            alerts.raise_alert(db, "budget_exceeded", f"Budget '{b.name}' exceeded: {label}", "error", f"budget:{b.id}:{slot}:100")
            raised.append(f"{b.name}:100")
        elif pct >= b.alert_threshold_percent:
            alerts.raise_alert(db, "budget_threshold", f"Budget '{b.name}' at {pct}%: {label}", "warning",
                               f"budget:{b.id}:{slot}:{b.alert_threshold_percent}")
            raised.append(f"{b.name}:{b.alert_threshold_percent}")
        today_spend = spent(db, today, today + timedelta(days=1), b.category)
        avg = spent(db, today - timedelta(days=7), today, b.category) / 7
        if avg > 0 and today_spend > avg * b.spike_multiplier:
            alerts.raise_alert(db, "usage_spike", f"Unusual usage for '{b.name}': today ${today_spend:,.2f} vs 7-day average ${avg:,.2f}",
                               "warning", f"spike:{b.id}:{today.date().isoformat()}")
            raised.append(f"{b.name}:spike")
    db.flush()
    return raised

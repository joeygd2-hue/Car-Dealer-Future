"""Usage and cost tracking for AI and other integrations (Phase 4, Step 2).

Cost basis vocabulary:
* verified_billing — amount copied from the provider's own billing/invoice data.
* estimated        — measured usage x our recorded unit rate. Not an invoice.
* unknown          — usage measured but no rate on file; cost not claimed.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.costs import Budget, ProviderRate, UsageRecord
from app.services import alerts

# Default unit rates used only for *estimates*. Each must be re-verified against
# the provider's current pricing page before being relied upon.
# Anthropic rates: per-1M-token list prices for the first-party API (cached reference 2026-10-06).
DEFAULT_RATES = [
    # provider, metric, price_micros, per_units, source_url, note
    ("anthropic:claude-opus-5-5", "input_tokens", 4_000_000, 1_000_000, "https://www.anthropic.com/pricing", "$4 / MTok"),
    ("anthropic:claude-opus-5-5", "output_tokens", 20_000_000, 1_000_000, "https://www.anthropic.com/pricing", "$20 / MTok"),
    ("anthropic:claude-opus-5-5", "cache_read_tokens", 200_000, 1_000_000, "https://www.anthropic.com/pricing", "$0.20 / MTok"),
    ("anthropic:claude-opus-5-5", "cache_write_tokens", 5_000_000, 1_000_000, "https://www.anthropic.com/pricing", "1.25x input (5-min cache) — verify"),
    ("anthropic:claude-sonnet-5-5", "input_tokens", 2_000_000, 1_000_000, "https://www.anthropic.com/pricing", "$2 / MTok"),
    ("anthropic:claude-sonnet-5-5", "output_tokens", 10_000_000, 1_000_000, "https://www.anthropic.com/pricing", "$10 / MTok"),
    ("anthropic:claude-sonnet-5-5", "cache_read_tokens", 200_000, 1_000_000, "https://www.anthropic.com/pricing", "$0.20 / MTok"),
    ("anthropic:claude-haiku-5-5", "input_tokens", 100_000, 1_000_000, "https://www.anthropic.com/pricing", "$0.10 / MTok (<=100K prompt)"),
    ("anthropic:claude-haiku-5-5", "output_tokens", 500_000, 1_000_000, "https://www.anthropic.com/pricing", "$0.50 / MTok (<=100K prompt)"),
    # Google Places API (New) Text Search — SKU price depends on field mask and volume tier. Placeholder estimate.
    ("google:places_text_search", "requests", 32_000, 1, "https://developers.google.com/maps/billing-and-pricing/pricing", "ESTIMATE ONLY: verify SKU tier and free monthly allowance"),
    # PageSpeed Insights API is free of charge within quota.
    ("google:pagespeed", "requests", 0, 1, "https://developers.google.com/speed/docs/insights/v5/get-started", "No charge within quota"),
]


def seed_default_rates(db: Session) -> int:
    added = 0
    for provider, metric, price, per, url, note in DEFAULT_RATES:
        exists = db.scalar(select(ProviderRate.id).where(ProviderRate.provider == provider, ProviderRate.metric == metric))
        if exists:
            continue
        db.add(
            ProviderRate(
                provider=provider,
                metric=metric,
                price_micros=price,
                per_units=per,
                effective_from=date(2026, 1, 1),
                source_url=url,
                verified_on=None,
                note=note,
            )
        )
        added += 1
    db.flush()
    return added


def current_rate(db: Session, provider: str, metric: str, on: date | None = None) -> ProviderRate | None:
    on = on or utcnow().date()
    return db.scalar(
        select(ProviderRate)
        .where(ProviderRate.provider == provider, ProviderRate.metric == metric, ProviderRate.effective_from <= on)
        .order_by(ProviderRate.effective_from.desc())
        .limit(1)
    )


def record_usage(
    db: Session,
    *,
    provider: str,
    service_category: str,
    metric: str,
    quantity: int,
    client_id: int | None = None,
    feature: str | None = None,
    idempotency_key: str | None = None,
    verified_cost_micros: int | None = None,
    occurred_at: datetime | None = None,
    details: dict | None = None,
) -> UsageRecord:
    """Record measured usage. Duplicate idempotency keys return the original row."""
    if idempotency_key:
        existing = db.scalar(select(UsageRecord).where(UsageRecord.idempotency_key == idempotency_key))
        if existing:
            return existing
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    rate = None
    if verified_cost_micros is not None:
        basis, cost = "verified_billing", verified_cost_micros
    else:
        rate = current_rate(db, provider, metric)
        if rate is None:
            basis, cost = "unknown", None
        else:
            # Round half up to the nearest micro-unit.
            basis, cost = "estimated", (quantity * rate.price_micros + rate.per_units // 2) // rate.per_units
    rec = UsageRecord(
        provider=provider,
        service_category=service_category,
        metric=metric,
        quantity=quantity,
        cost_micros=cost,
        cost_basis=basis,
        rate_id=rate.id if rate else None,
        client_id=client_id,
        feature=feature,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at or utcnow(),
        details=details or {},
    )
    db.add(rec)
    db.flush()
    return rec


def import_verified_costs_csv(db: Session, content: str, provider: str, service_category: str) -> dict:
    """Import provider billing export rows: date,metric,quantity,cost (cost in currency units, e.g. 12.34)."""
    reader = csv.DictReader(io.StringIO(content))
    created, errors = 0, []
    for i, row in enumerate(reader, start=2):
        try:
            d = date.fromisoformat(row["date"].strip())
            metric = row["metric"].strip()
            qty = int(float(row.get("quantity") or 0))
            cost_micros = int(round(float(row["cost"]) * 1_000_000))
            key = f"billing:{provider}:{d.isoformat()}:{metric}"
            before = db.scalar(select(UsageRecord.id).where(UsageRecord.idempotency_key == key))
            record_usage(
                db,
                provider=provider,
                service_category=service_category,
                metric=metric,
                quantity=qty,
                verified_cost_micros=cost_micros,
                idempotency_key=key,
                occurred_at=datetime.combine(d, datetime.min.time()),
                details={"source": "billing_import"},
            )
            if before is None:
                created += 1
        except (KeyError, ValueError) as exc:
            errors.append(f"line {i}: {exc}")
    return {"created": created, "errors": errors}


def usage_summary(db: Session, start: date, end: date, client_id: int | None = None) -> dict:
    """Totals grouped by provider and cost basis. Verified and estimated amounts are never merged."""
    stmt = (
        select(
            UsageRecord.provider,
            UsageRecord.service_category,
            UsageRecord.metric,
            UsageRecord.cost_basis,
            func.sum(UsageRecord.quantity),
            func.sum(UsageRecord.cost_micros),
            func.count(),
        )
        .where(
            UsageRecord.occurred_at >= datetime.combine(start, datetime.min.time()),
            UsageRecord.occurred_at < datetime.combine(end + timedelta(days=1), datetime.min.time()),
        )
        .group_by(UsageRecord.provider, UsageRecord.service_category, UsageRecord.metric, UsageRecord.cost_basis)
        .order_by(UsageRecord.provider, UsageRecord.metric)
    )
    if client_id is not None:
        stmt = stmt.where(UsageRecord.client_id == client_id)
    rows = []
    totals = {"verified_billing": 0, "estimated": 0, "unknown_records": 0}
    for provider, cat, metric, basis, qty, cost, n in db.execute(stmt):
        rows.append(
            {
                "provider": provider,
                "category": cat,
                "metric": metric,
                "cost_basis": basis,
                "quantity": int(qty or 0),
                "cost_micros": int(cost) if cost is not None else None,
                "records": n,
            }
        )
        if basis == "unknown":
            totals["unknown_records"] += n
        else:
            totals[basis] += int(cost or 0)
    return {"rows": rows, "totals": totals}


def spend_micros(db: Session, start: datetime, end: datetime, provider: str | None = None, category: str | None = None) -> int:
    stmt = select(func.coalesce(func.sum(UsageRecord.cost_micros), 0)).where(
        UsageRecord.occurred_at >= start, UsageRecord.occurred_at < end
    )
    if provider:
        stmt = stmt.where(or_(UsageRecord.provider == provider, UsageRecord.provider.like(f"{provider}:%")))
    if category:
        stmt = stmt.where(UsageRecord.service_category == category)
    return int(db.scalar(stmt) or 0)


def check_budgets(db: Session, now: datetime | None = None) -> list[str]:
    """Raise alerts for budgets crossing their threshold and for usage spikes."""
    now = now or utcnow()
    raised: list[str] = []
    today = datetime(now.year, now.month, now.day)
    for b in db.scalars(select(Budget).where(Budget.active.is_(True))):
        if b.period == "daily":
            start, end, slot = today, today + timedelta(days=1), today.date().isoformat()
        else:
            start = datetime(now.year, now.month, 1)
            end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
            slot = f"{now.year}-{now.month:02d}"
        spent = spend_micros(db, start, end, b.provider, b.service_category)
        pct = (spent * 100 // b.amount_micros) if b.amount_micros else 0
        if pct >= 100:
            alerts.raise_alert(
                db, "budget_exceeded", f"Budget '{b.name}' exceeded: {fmt_micros(spent)} of {fmt_micros(b.amount_micros)} "
                f"({b.period}, includes estimates)", "error", f"budget:{b.id}:{slot}:100"
            )
            raised.append(f"{b.name}:100")
        elif pct >= b.alert_threshold_percent:
            alerts.raise_alert(
                db, "budget_threshold", f"Budget '{b.name}' at {pct}%: {fmt_micros(spent)} of {fmt_micros(b.amount_micros)} "
                f"({b.period}, includes estimates)", "warning", f"budget:{b.id}:{slot}:{b.alert_threshold_percent}"
            )
            raised.append(f"{b.name}:{b.alert_threshold_percent}")
        # Spike detection: today's spend vs trailing 7-day daily average.
        today_spend = spend_micros(db, today, today + timedelta(days=1), b.provider, b.service_category)
        trailing = spend_micros(db, today - timedelta(days=7), today, b.provider, b.service_category)
        avg = trailing / 7
        if avg > 0 and today_spend > avg * b.spike_multiplier:
            alerts.raise_alert(
                db, "usage_spike", f"Unusual usage for '{b.name}': today {fmt_micros(today_spend)} vs 7-day average "
                f"{fmt_micros(int(avg))}", "warning", f"spike:{b.id}:{today.date().isoformat()}"
            )
            raised.append(f"{b.name}:spike")
    db.flush()
    return raised


def fmt_micros(m: int | None, currency: str = "USD") -> str:
    if m is None:
        return "unknown"
    return f"${m / 1_000_000:,.4f}" if abs(m) < 10_000 else f"${m / 1_000_000:,.2f}"

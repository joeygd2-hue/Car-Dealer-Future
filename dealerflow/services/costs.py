"""Operating cost tracking and budget checks. Estimates are labelled as estimates."""

from __future__ import annotations


from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import CostEntry
from .settings_store import get_setting


def record(db: Session, category: str, amount: float, *, estimate: bool, lead_source: str | None = None,
           note: str = "", ref: str | None = None) -> CostEntry | None:
    if amount <= 0:
        return None
    row = CostEntry(incurred_on=utcnow(), category=category, amount=round(amount, 4), is_estimate=estimate,
                    lead_source=lead_source, note=note[:500], ref=ref)
    db.add(row)
    return row


def estimate(db: Session, key: str, units: float = 1) -> float:
    return float(get_setting(db, "cost_estimates").get(key, 0.0)) * units


def spent_since(db: Session, since) -> float:
    return float(db.scalar(select(func.coalesce(func.sum(CostEntry.amount), 0)).where(CostEntry.incurred_on >= since)) or 0)


def spent_today(db: Session) -> float:
    return spent_since(db, utcnow().replace(hour=0, minute=0, second=0, microsecond=0))


def spent_month(db: Session) -> float:
    return spent_since(db, utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def within_budget(db: Session, extra: float, daily: float, monthly: float) -> tuple[bool, str]:
    t, m = spent_today(db), spent_month(db)
    if t + extra > daily:
        return False, f"Daily budget ${daily:.2f} would be exceeded (spent ${t:.2f}, next action ~${extra:.2f})"
    if m + extra > monthly:
        return False, f"Monthly budget ${monthly:.2f} would be exceeded (spent ${m:.2f}, next action ~${extra:.2f})"
    return True, ""



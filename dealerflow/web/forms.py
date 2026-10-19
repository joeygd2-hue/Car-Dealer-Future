"""Form parsing and template filters shared by the Phase 4 pages."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .deps import templates


def parse_money(value: str | None, field: str = "amount") -> int:
    """'1,234.56' / '$1234' -> 123456 cents. Rejects more than two decimal places."""
    raw = re.sub(r"[,$\s]", "", value or "")
    try:
        d = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {field}") from exc
    if d.as_tuple().exponent < -2:
        raise ValueError(f"{field} has more than 2 decimal places")
    return int(d * 100)


def parse_date(value: str | None, field: str = "date") -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {field} (use YYYY-MM-DD)") from exc


def money(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def dollars(amount: float | None) -> str:
    if amount is None:
        return "—"
    return f"${amount:,.4f}" if 0 < abs(amount) < 1 else f"${amount:,.2f}"


templates.env.filters["money"] = money
templates.env.filters["dollars"] = dollars
templates.env.filters["label"] = lambda s: (s or "").replace("_", " ")

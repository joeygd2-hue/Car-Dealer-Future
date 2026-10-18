"""Shared web helpers: templates, money formatting/parsing, flash messages."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.security.auth import AuthContext

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def money(cents: int | None, currency: str = "USD") -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def micros(m: int | None) -> str:
    if m is None:
        return "unknown"
    v = m / 1_000_000
    return f"${v:,.4f}" if abs(v) < 1 else f"${v:,.2f}"


TEMPLATES.env.filters["money"] = money
TEMPLATES.env.filters["micros"] = micros
TEMPLATES.env.filters["label"] = lambda s: (s or "").replace("_", " ")


def parse_money(value: str | None, field: str = "amount") -> int:
    """Parse '1,234.56' / '$1234' to integer cents. Rejects more than 2 decimals."""
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


def render(request: Request, name: str, ctx: AuthContext | None, **kw) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        name,
        {"user": ctx.user if ctx else None, "csrf_token": ctx.csrf_token if ctx else "", "msg": request.query_params.get("msg"),
         "err": request.query_params.get("err"), **kw},
    )


def redirect(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)

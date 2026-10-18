"""Shared web helpers: templates, scoped dependencies, flash messages, simple rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Dealership, Membership, Notification
from app.security.auth import AuthContext, DealershipScope, get_auth, require_platform_admin, resolve_scope, verify_csrf

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _money(cents):
    return "—" if cents is None else f"${cents / 100:,.0f}"


def _dt(value):
    return value.strftime("%Y-%m-%d %H:%M") + " UTC" if value else "—"


TEMPLATES.env.filters["money"] = _money
TEMPLATES.env.filters["dt"] = _dt


def render(request: Request, name: str, db: Session | None = None, **ctx):
    auth: AuthContext | None = getattr(request.state, "auth", None)
    dealerships = []
    unread = 0
    if auth and db is not None:
        if auth.user.is_platform_admin:
            dealerships = list(db.scalars(select(Dealership).order_by(Dealership.name)))
        else:
            dealerships = [m.dealership for m in db.scalars(select(Membership).where(Membership.user_id == auth.user.id))]
        unread = db.query(Notification).filter(Notification.user_id == auth.user.id, Notification.channel == "IN_APP",
                                               Notification.read_at.is_(None)).count()
    return TEMPLATES.TemplateResponse(request, name, {
        "auth": auth, "user": auth.user if auth else None, "csrf_token": auth.csrf_token if auth else "",
        "nav_dealerships": dealerships, "unread": unread, "flash": request.query_params.get("msg"),
        "flash_error": request.query_params.get("err"), "settings": get_settings(), **ctx})


def redirect(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
        sep = "&"
    if err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


def portal_scope(dealership_id: int, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)) -> DealershipScope:
    return resolve_scope(db, auth, dealership_id)


async def portal_scope_post(dealership_id: int, auth: AuthContext = Depends(verify_csrf),
                            db: Session = Depends(get_db)) -> DealershipScope:
    return resolve_scope(db, auth, dealership_id)


def dealer_admin_post(scope: DealershipScope = Depends(portal_scope_post)) -> DealershipScope:
    if not scope.is_admin:
        raise HTTPException(status_code=403, detail="Dealership administrator role required")
    return scope


async def admin_post(auth: AuthContext = Depends(verify_csrf)) -> AuthContext:
    if not auth.user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrator access required")
    return auth


admin_get = require_platform_admin


class RateLimiter:
    """In-process sliding-window limiter (per key). Use a shared store (e.g. Redis) when running several web processes."""

    def __init__(self, limit: int, window_seconds: float):
        self.limit, self.window = limit, window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

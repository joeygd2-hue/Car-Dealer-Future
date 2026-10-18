"""Authentication, role checks, CSRF and template helpers."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict
from datetime import timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import User
from ..security import hash_api_key, role_at_least
from ..services.normalize import format_phone

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _local(dt, fmt: str = "%Y-%m-%d %H:%M"):
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(get_settings().timezone)).strftime(fmt)


templates.env.filters["local"] = _local
templates.env.filters["phone"] = format_phone


class NotAuthenticated(Exception):
    pass


# Paths a client (dealership) user may reach. Everything else is staff-only, enforced centrally here so
# that a route using plain Depends(current_user) can never leak internal data to a client user.
CLIENT_ALLOWED_PREFIXES = ("/portal", "/api/v1/portal", "/logout")


def _confine_client(request: Request, user: User) -> None:
    if user.is_client and not request.url.path.startswith(CLIENT_ALLOWED_PREFIXES):
        raise HTTPException(403, "This area is for staff. Use the client portal.")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        user = db.scalar(select(User).where(User.api_key_hash == hash_api_key(auth[7:].strip()), User.active.is_(True)))
        if not user:
            raise HTTPException(401, "Invalid API key")
        request.state.api = True
        _confine_client(request, user)
        return user
    uid = request.session.get("uid")
    if not uid:
        raise NotAuthenticated()
    user = db.get(User, uid)
    if not user or not user.active:
        request.session.clear()
        raise NotAuthenticated()
    if time.time() - request.session.get("t", 0) > get_settings().session_max_age_seconds:
        request.session.clear()
        raise NotAuthenticated()
    request.state.api = False
    _confine_client(request, user)
    return user


def require_client(user: User = Depends(current_user)) -> User:
    """Client-portal dependency: only client users bound to a client."""
    if not user.is_client or user.client_id is None:
        raise HTTPException(403, "Client portal only")
    return user


def require(role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not role_at_least(user.role, role):
            raise HTTPException(403, f"Requires {role} role")
        return user

    return dep


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def check_csrf(request: Request) -> None:
    """For cookie-authenticated form posts. Bearer-token API calls are exempt."""
    if request.method != "POST" or request.headers.get("authorization", "").lower().startswith("bearer "):
        return
    form = await request.form()
    sent = form.get("csrf") or request.headers.get("x-csrf-token")
    if not sent or not secrets.compare_digest(str(sent), request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF token missing or invalid; reload the page and try again")


def render(request: Request, name: str, user: User | None, **ctx: Any):
    flash = request.session.pop("flash", None)
    s = get_settings()
    return templates.TemplateResponse(request, name, {
        "user": user, "csrf": csrf_token(request), "flash": flash, "settings": s, "role_at_least": role_at_least,
        "providers": {"email": s.email_provider, "calendar": s.calendar_provider, "zoom": s.zoom_provider,
                      "call": s.call_provider}, **ctx})


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


class LoginLimiter:
    """At most 5 failed logins per email+IP in 15 minutes."""

    def __init__(self, limit: int = 5, window: float = 900):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = defaultdict(list)

    def blocked(self, key: str) -> bool:
        now = time.time()
        self.fails[key] = [t for t in self.fails[key] if now - t < self.window]
        return len(self.fails[key]) >= self.limit

    def fail(self, key: str) -> None:
        self.fails[key].append(time.time())

    def reset(self, key: str) -> None:
        self.fails.pop(key, None)


login_limiter = LoginLimiter()

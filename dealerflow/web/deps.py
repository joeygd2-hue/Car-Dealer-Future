"""Authentication, role checks, CSRF and template helpers."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
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


def _safe_url(url: str | None) -> str:
    """Only http(s) URLs become links; anything else (javascript:, data:) renders as '#'."""
    if url and urlparse(url.strip()).scheme in ("http", "https"):
        return url.strip()
    return "#"


def safe_next(target: str | None, default: str = "/") -> str:
    """Same-site relative path only (blocks //evil.com and /\\evil.com)."""
    if not target:
        return default
    parsed = urlparse(target)
    path = parsed.path or "/"
    if parsed.scheme or parsed.netloc or not path.startswith("/") or path.startswith("//") or "\\" in target:
        return default
    return path + (f"?{parsed.query}" if parsed.query else "") + (f"#{parsed.fragment}" if parsed.fragment else "")


def back_to(request: Request, default: str = "/") -> str:
    """Path from the Referer header when it points at this same host; otherwise ``default``."""
    ref = urlparse(request.headers.get("referer") or "")
    if ref.netloc and ref.netloc == request.url.netloc:
        return safe_next(ref.path + (f"?{ref.query}" if ref.query else ""), default)
    return default


templates.env.filters["local"] = _local
templates.env.filters["safe_url"] = _safe_url
templates.env.filters["phone"] = format_phone


class NotAuthenticated(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        user = db.scalar(select(User).where(User.api_key_hash == hash_api_key(auth[7:].strip()), User.active.is_(True)))
        if not user:
            raise HTTPException(401, "Invalid API key")
        request.state.api = True
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

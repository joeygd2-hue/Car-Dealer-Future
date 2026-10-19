"""Authentication, role checks, CSRF and template helpers."""

from __future__ import annotations

import secrets
import time
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
from ..models import User, UserSession
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


class DealerHome(Exception):
    """A dealership user opened a staff page; send them to their own workspace."""

    def __init__(self, client_id: int):
        self.client_id = client_id


# Dealership (client) users may only use their own workspace. Everything else is staff-only.
# /portal holds Phase 4 reports, invoices, SEO findings and support requests for the same dealership.
DEALER_ALLOWED_PREFIXES = ("/dealers", "/api/v1/dealers", "/portal", "/api/v1/portal", "/logout", "/account/")


def _restrict_dealer_user(request: Request, user: User) -> None:
    if user.client_id is None:
        return
    path = request.url.path
    if path.startswith(DEALER_ALLOWED_PREFIXES):
        return
    if path == "/" and request.method == "GET":
        raise DealerHome(user.client_id)
    raise HTTPException(403, "This area is only available to agency staff")


def password_version(user: User) -> str:
    """Short fingerprint of the password hash; changing the password ends every existing session."""
    import hashlib

    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        user = db.scalar(select(User).where(User.api_key_hash == hash_api_key(auth[7:].strip()), User.active.is_(True)))
        if not user:
            raise HTTPException(401, "Invalid API key")
        request.state.api = True
        _restrict_dealer_user(request, user)
        return user
    user, row = session_user(request, db)
    if not row.mfa_verified:
        raise MfaRequired()
    if (admin_mfa_required() and user.client_id is None and user.role == "admin" and not user.mfa_enabled
            and not request.url.path.startswith(MFA_ENROLL_PATHS)):
        raise MfaEnrollmentRequired()
    request.state.api = False
    _restrict_dealer_user(request, user)
    return user


class MfaRequired(Exception):
    """Password accepted but the second factor has not been verified for this session."""


class MfaEnrollmentRequired(Exception):
    """Policy requires MFA for agency admins and this admin has not enrolled yet."""


MFA_ENROLL_PATHS = ("/account/security", "/account/mfa", "/logout")


def admin_mfa_required() -> bool:
    s = get_settings()
    return s.require_admin_mfa if s.require_admin_mfa is not None else s.env == "production"


def session_user(request: Request, db: Session) -> tuple[User, "UserSession"]:
    """The user and server-side session row behind the cookie (MFA not checked here)."""
    from .. import sessions

    uid = request.session.get("uid")
    if not uid:
        raise NotAuthenticated()
    row = sessions.load(db, request.session.get("sid"))
    user = db.get(User, uid)
    if row is None or row.user_id != uid or not user or not user.active:
        request.session.clear()
        raise NotAuthenticated()
    if time.time() - request.session.get("t", 0) > get_settings().session_max_age_seconds:
        request.session.clear()
        raise NotAuthenticated()
    if request.session.get("pv") != password_version(user):
        request.session.clear()  # password changed since this session started
        raise NotAuthenticated()
    return user, row


def pending_mfa_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Only for the second-factor page: a session whose password step passed but MFA did not yet."""
    user, _row = session_user(request, db)
    return user


def require_client(user: User = Depends(current_user)) -> User:
    """Client-portal dependency: only dealership users bound to a client."""
    if user.client_id is None:
        raise HTTPException(403, "Client portal only")
    return user


def require(role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.client_id is not None or not role_at_least(user.role, role):
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
    """At most 5 failed logins per email+IP per 15-minute window, counted in the database (``rate_counters``) so
    every web process shares the same count."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit, self.window = limit, window

    def blocked(self, db: Session, key: str) -> bool:
        from ..services.customer import rate_limits

        return rate_limits.current(db, f"login_fail:{key}", self.window) >= self.limit

    def fail(self, db: Session, key: str) -> None:
        from ..services.customer import rate_limits

        rate_limits.try_acquire(db, f"login_fail:{key}", 10**9, self.window)
        db.commit()

    def reset(self, db: Session, key: str) -> None:
        from sqlalchemy import delete

        from ..models_customer import RateCounter

        db.execute(delete(RateCounter).where(RateCounter.key == f"login_fail:{key}"))


login_limiter = LoginLimiter()

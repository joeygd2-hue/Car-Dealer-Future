"""Authentication, authorization and tenant (client) isolation.

Two credentials are accepted:
* Session cookie (browser). State-changing requests must carry the session's
  CSRF token (form field `csrf_token` or header `X-CSRF-Token`).
* `Authorization: Bearer <api token>` (server-to-server). No CSRF needed.

Isolation rule: client users are bound to exactly one client. Every query for
tenant-owned data made on behalf of a client user MUST go through
`scope_to_user()` / `get_owned_or_404()`, which add `client_id = user.client_id`.
Staff routes reject client users outright.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.base import utcnow
from app.models.core import ADMIN_ROLES, STAFF_ROLES, ApiToken, User, UserSession
from app.security.crypto import new_token, sha256_hex, verify_password

SESSION_COOKIE = "dgs_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCKOUT_THRESHOLD = 8
LOCKOUT_MINUTES = 15


@dataclass
class AuthContext:
    user: User
    via: str  # "session" | "token"
    session: UserSession | None = None

    @property
    def csrf_token(self) -> str | None:
        return self.session.csrf_token if self.session else None


class LoginError(Exception):
    pass


def authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    now = utcnow()
    if user is None:
        verify_password(password, "scrypt$16384$8$1$00$00")  # equalize timing
        raise LoginError("Invalid email or password")
    if user.locked_until and user.locked_until > now:
        raise LoginError("Account temporarily locked. Try again later.")
    if not user.is_active or not verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= LOCKOUT_THRESHOLD:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_logins = 0
        db.commit()
        raise LoginError("Invalid email or password")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    return user


def create_session(db: Session, user: User, ip: str | None, user_agent: str | None) -> str:
    token = new_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=sha256_hex(token),
            csrf_token=new_token(24),
            expires_at=utcnow() + timedelta(hours=get_settings().session_ttl_hours),
            ip=ip,
            user_agent=(user_agent or "")[:300],
        )
    )
    db.commit()
    return token


def revoke_session(db: Session, token: str) -> None:
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == sha256_hex(token)))
    if sess and not sess.revoked_at:
        sess.revoked_at = utcnow()
        db.commit()


def create_api_token(db: Session, user: User, name: str, days: int | None = 90) -> str:
    token = "dgs_" + new_token()
    db.add(
        ApiToken(
            user_id=user.id,
            name=name,
            token_hash=sha256_hex(token),
            prefix=token[:8],
            expires_at=utcnow() + timedelta(days=days) if days else None,
        )
    )
    db.commit()
    return token


def _resolve(request: Request, db: Session) -> AuthContext | None:
    now = utcnow()
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        raw = header[7:].strip()
        tok = db.scalar(select(ApiToken).where(ApiToken.token_hash == sha256_hex(raw)))
        if tok is None or tok.revoked_at or (tok.expires_at and tok.expires_at < now):
            raise HTTPException(status_code=401, detail="Invalid or expired API token")
        if not tok.user.is_active:
            raise HTTPException(status_code=401, detail="User disabled")
        tok.last_used_at = now
        db.commit()
        return AuthContext(user=tok.user, via="token")
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == sha256_hex(cookie)))
    if sess is None or sess.revoked_at or sess.expires_at < now or not sess.user.is_active:
        return None
    return AuthContext(user=sess.user, via="session", session=sess)


async def _check_csrf(request: Request, ctx: AuthContext) -> None:
    if ctx.via != "session" or request.method in SAFE_METHODS:
        return
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if "form" in ctype:
            form = await request.form()
            supplied = form.get("csrf_token")  # type: ignore[assignment]
    import hmac

    if not supplied or not hmac.compare_digest(str(supplied), ctx.session.csrf_token):  # type: ignore[union-attr]
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


async def optional_auth(request: Request, db: Session = Depends(get_db)) -> AuthContext | None:
    ctx = _resolve(request, db)
    if ctx:
        request.state.user_id = ctx.user.id
    return ctx


async def require_auth(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    ctx = _resolve(request, db)
    if ctx is None:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="Authentication required")
        raise HTTPException(status_code=303, headers={"Location": f"/login?next={request.url.path}"})
    await _check_csrf(request, ctx)
    request.state.user_id = ctx.user.id
    return ctx


def require_roles(*roles: str):
    async def dep(ctx: AuthContext = Depends(require_auth)) -> AuthContext:
        if ctx.user.role not in roles:
            raise HTTPException(status_code=403, detail="You do not have permission to access this resource")
        return ctx

    return dep


require_staff = require_roles(*STAFF_ROLES)
require_admin = require_roles(*ADMIN_ROLES)
require_client_user = require_roles("client")


def scope_to_user(stmt: Any, model: Any, user: User) -> Any:
    """Restrict a SELECT on a tenant-owned model to what `user` may see."""
    if user.is_staff:
        return stmt
    if user.client_id is None:  # defensive: a client user without a client sees nothing
        return stmt.where(False)
    return stmt.where(model.client_id == user.client_id)


def get_owned_or_404(db: Session, model: Any, obj_id: int, user: User, client_id: int | None = None) -> Any:
    """Fetch a tenant-owned row, returning 404 (not 403) when outside the caller's scope."""
    stmt = select(model).where(model.id == obj_id)
    stmt = scope_to_user(stmt, model, user)
    if client_id is not None:
        stmt = stmt.where(model.client_id == client_id)
    obj = db.scalar(stmt)
    if obj is None:
        raise HTTPException(status_code=404, detail="Not found")
    return obj

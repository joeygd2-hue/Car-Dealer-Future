"""Session authentication, CSRF and server-side tenant authorization.

Every portal route resolves the dealership through ``require_dealership_access`` and every
query for tenant data must filter by that dealership id (use ``get_owned``).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeVar

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import Dealership, Membership, User, UserSession
from app.security.passwords import verify_password

SESSION_COOKIE = "cdf_session"
T = TypeVar("T")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return None
    return user


def create_session(db: Session, user: User, ip: str | None = None, user_agent: str | None = None,
                   *, mfa_verified: bool | None = None) -> str:
    """New random token per login (no fixation). Sessions of MFA users start unverified."""
    token = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            token_hash=_hash_token(token),
            user_id=user.id,
            csrf_token=secrets.token_urlsafe(24),
            mfa_verified=(not user.mfa_enabled) if mfa_verified is None else mfa_verified,
            expires_at=utcnow() + timedelta(hours=get_settings().session_ttl_hours),
            ip=ip,
            user_agent=(user_agent or "")[:300],
        )
    )
    user.last_login_at = utcnow()
    db.commit()
    return token


def invalidate_sessions(db: Session, user_id: int, *, except_token: str | None = None) -> int:
    keep = _hash_token(except_token) if except_token else None
    n = 0
    for sess in db.scalars(select(UserSession).where(UserSession.user_id == user_id)):
        if sess.token_hash != keep:
            db.delete(sess)
            n += 1
    return n


def session_from_token(db: Session, token: str | None) -> UserSession | None:
    if not token:
        return None
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == _hash_token(token)))
    if not sess or sess.expires_at < utcnow():
        return None
    return sess


def destroy_session(db: Session, token: str | None) -> None:
    if not token:
        return
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == _hash_token(token)))
    if sess:
        db.delete(sess)
        db.commit()


@dataclass
class AuthContext:
    user: User
    session: UserSession

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token


class LoginRequired(Exception):
    """Raised for HTML routes; converted to a redirect to /login by an exception handler."""


class MfaRequired(Exception):
    """Password accepted but the second factor has not been verified for this session."""


class MfaEnrollmentRequired(Exception):
    """Policy requires MFA for this user (platform admins in production) and it is not enrolled."""


MFA_ENROLL_PATHS = ("/account/security", "/logout")


def _load(request: Request, db: Session) -> AuthContext:
    sess = session_from_token(db, request.cookies.get(SESSION_COOKIE))
    if sess is None:
        raise LoginRequired()
    user = db.get(User, sess.user_id)
    if not user or not user.is_active:
        raise LoginRequired()
    return AuthContext(user=user, session=sess)


def get_auth(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    ctx = _load(request, db)
    if not ctx.session.mfa_verified:
        raise MfaRequired()
    if (get_settings().admin_mfa_required and ctx.user.is_platform_admin and not ctx.user.mfa_enabled
            and not request.url.path.startswith(MFA_ENROLL_PATHS)):
        raise MfaEnrollmentRequired()
    request.state.auth = ctx
    return ctx


def get_auth_pending_mfa(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    """Only for the MFA challenge page: accepts a session whose second factor is not yet verified."""
    return _load(request, db)


async def verify_csrf(request: Request, auth: AuthContext = Depends(get_auth)) -> AuthContext:
    """Dependency for state-changing form posts."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        form = await request.form()
        sent = form.get("csrf_token") or request.headers.get("x-csrf-token")
        if not sent or not secrets.compare_digest(str(sent), auth.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    return auth


def require_platform_admin(auth: AuthContext = Depends(get_auth)) -> AuthContext:
    if not auth.user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrator access required")
    return auth


@dataclass
class DealershipScope:
    auth: AuthContext
    dealership: Dealership
    role: str  # PLATFORM_ADMIN | DEALER_ADMIN | DEALER_STAFF

    @property
    def user(self) -> User:
        return self.auth.user

    @property
    def is_admin(self) -> bool:
        return self.role in ("PLATFORM_ADMIN", "DEALER_ADMIN")


def resolve_scope(db: Session, auth: AuthContext, dealership_id: int) -> DealershipScope:
    dealership = db.get(Dealership, dealership_id)
    if dealership is None:
        raise HTTPException(status_code=404, detail="Not found")
    if auth.user.is_platform_admin:
        return DealershipScope(auth, dealership, "PLATFORM_ADMIN")
    membership = db.scalar(
        select(Membership).where(Membership.user_id == auth.user.id, Membership.dealership_id == dealership_id)
    )
    if membership is None:
        # 404 rather than 403 so tenant ids cannot be enumerated.
        raise HTTPException(status_code=404, detail="Not found")
    return DealershipScope(auth, dealership, membership.role)


def require_dealership_access(
    dealership_id: int, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> DealershipScope:
    return resolve_scope(db, auth, dealership_id)


def require_dealer_admin(scope: DealershipScope) -> None:
    if not scope.is_admin:
        raise HTTPException(status_code=403, detail="Dealership administrator role required")


def get_owned(db: Session, model: type[T], object_id: int, dealership_id: int) -> T:
    """Fetch a tenant-owned row, raising 404 if it belongs to another dealership."""
    obj = db.get(model, object_id)
    if obj is None or getattr(obj, "dealership_id", None) != dealership_id:
        raise HTTPException(status_code=404, detail="Not found")
    return obj

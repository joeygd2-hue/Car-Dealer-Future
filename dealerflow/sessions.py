"""Server-side login sessions.

The signed session cookie (Starlette ``SessionMiddleware``) carries only a random session id (``sid``), the CSRF
token and flash messages; the session itself lives in ``user_sessions`` (SHA-256 of the id). That makes logout,
"end all sessions", password resets and admin deactivation take effect immediately on every device, and holds the
MFA-pending state between the password and the second factor.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .models import User, UserSession

IDLE_TIMEOUT = timedelta(hours=2)


def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def create(db: Session, user: User, *, ip: str | None, user_agent: str | None, mfa_verified: bool) -> str:
    sid = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(UserSession(token_hash=_hash(sid), user_id=user.id, mfa_verified=mfa_verified, created_at=now, last_seen_at=now,
                       expires_at=now + timedelta(seconds=get_settings().session_max_age_seconds), ip=(ip or "")[:64],
                       user_agent=(user_agent or "")[:300]))
    db.flush()
    return sid


def load(db: Session, sid: str | None) -> UserSession | None:
    if not sid:
        return None
    row = db.scalar(select(UserSession).where(UserSession.token_hash == _hash(sid)))
    now = utcnow()
    if row is None or row.expires_at < now or row.last_seen_at < now - IDLE_TIMEOUT:
        return None
    if (now - row.last_seen_at).total_seconds() > 60:  # sliding idle window without a write per request
        db.execute(update(UserSession).where(UserSession.id == row.id).values(last_seen_at=now))
        db.commit()
    return row


def mark_mfa_verified(db: Session, row: UserSession) -> None:
    row.mfa_verified = True
    db.flush()


def destroy(db: Session, sid: str | None) -> None:
    if sid:
        db.execute(delete(UserSession).where(UserSession.token_hash == _hash(sid)))


def end_all(db: Session, user_id: int, *, except_sid: str | None = None) -> int:
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if except_sid:
        stmt = stmt.where(UserSession.token_hash != _hash(except_sid))
    return db.execute(stmt).rowcount or 0


def purge_expired(db: Session) -> int:
    now = utcnow()
    return db.execute(delete(UserSession).where((UserSession.expires_at < now) | (UserSession.last_seen_at < now - IDLE_TIMEOUT))).rowcount or 0

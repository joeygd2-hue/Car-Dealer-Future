from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import BadRequest, Unauthorized
from app.core.time import sha256, utcnow
from app.models import AuthSession, User

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 10:
        raise BadRequest("Password must be at least 10 characters")
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=64)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, hash_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    expected = base64.b64decode(hash_b64)
    actual = hashlib.scrypt(str(password).encode(), salt=base64.b64decode(salt_b64), n=_N, r=_R, p=_P, dklen=len(expected))
    return hmac.compare_digest(expected, actual)


_DUMMY = hash_password("not-a-real-password-0000")


def create_user(db: Session, *, email: str, name: str, password: str, platform_role: str | None = None) -> User:
    user = User(email=email.strip().lower(), name=name, password_hash=hash_password(password), platform_role=platform_role)
    db.add(user)
    db.flush()
    return user


def login(db: Session, email: str, password: str) -> tuple[str, AuthSession, User]:
    user = db.scalar(select(User).where(User.email == (email or "").strip().lower()))
    ok = verify_password(password, user.password_hash) if user else (verify_password(password, _DUMMY) and False)
    if not ok or user.disabled:
        raise Unauthorized("Invalid email or password")
    token = secrets.token_urlsafe(32)
    sess = AuthSession(token_hash=sha256(token), user_id=user.id, expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours))
    db.add(sess)
    db.flush()
    return token, sess, user


def authenticate(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    sess = db.get(AuthSession, sha256(token))
    if not sess or sess.expires_at < utcnow():
        return None
    user = db.get(User, sess.user_id)
    return user if user and not user.disabled else None


def logout(db: Session, token: str) -> None:
    sess = db.get(AuthSession, sha256(token or ""))
    if sess:
        db.delete(sess)

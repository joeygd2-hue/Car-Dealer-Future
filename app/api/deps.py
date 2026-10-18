from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.errors import Unauthorized
from app.core.security import authenticate
from app.core.tenancy import ClientContext, authorize_client, require_platform
from app.db import SessionLocal
from app.models import User


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def bearer_token(authorization: str | None = Header(default=None)) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def current_user(db: Session = Depends(get_db), token: str | None = Depends(bearer_token)) -> User:
    user = authenticate(db, token)
    if not user:
        raise Unauthorized()
    return user


def client_ctx(permission: str = "view"):
    """Dependency factory: authorises the caller for {client_id} and yields a
    tenant-bound context whose session is committed after the handler."""
    def dep(client_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> Iterator[ClientContext]:
        ctx = authorize_client(db, user, client_id, permission)
        try:
            yield ctx
            ctx.db.commit()
        except Exception:
            ctx.db.rollback()
            raise
        finally:
            ctx.db.close()
    return dep


def platform_user(user: User = Depends(current_user)) -> User:
    require_platform(user)
    return user


def platform_admin(user: User = Depends(current_user)) -> User:
    require_platform(user, admin=True)
    return user

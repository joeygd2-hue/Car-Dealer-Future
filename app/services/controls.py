"""Global kill switches and live-operation authorization checks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import LiveAuthorization, SystemControl
from app.services import audit

GLOBAL_AUTOMATION_PAUSE = "automation.global_pause"
OUTREACH_EMERGENCY_STOP = "outreach.emergency_stop"


def get_flag(db: Session, key: str) -> bool:
    row = db.scalar(select(SystemControl).where(SystemControl.key == key))
    return bool(row and row.bool_value)


def set_flag(db: Session, key: str, value: bool, *, user_id: int | None, reason: str | None) -> None:
    row = db.scalar(select(SystemControl).where(SystemControl.key == key))
    if row is None:
        row = SystemControl(key=key)
        db.add(row)
    row.bool_value = value
    row.reason = reason
    row.updated_by_id = user_id
    row.updated_at = utcnow()
    audit.record(
        db, f"control.{key}.{'on' if value else 'off'}", actor_type="USER" if user_id else "SYSTEM",
        actor_user_id=user_id, reason=reason,
    )


def live_authorized(db: Session, scope: str, dealership_id: int | None = None) -> bool:
    """True only if the env master switch is on AND a human recorded an unrevoked authorization."""
    if not get_settings().live_integrations_enabled:
        return False
    stmt = select(LiveAuthorization).where(
        LiveAuthorization.scope == scope, LiveAuthorization.revoked_at.is_(None)
    )
    if dealership_id is not None:
        stmt = stmt.where(LiveAuthorization.dealership_id == dealership_id)
    return db.scalar(stmt.limit(1)) is not None


def authorize_live(db: Session, scope: str, *, user_id: int, statement: str, dealership_id: int | None = None) -> LiveAuthorization:
    auth = LiveAuthorization(scope=scope, statement=statement, authorized_by_id=user_id, dealership_id=dealership_id)
    db.add(auth)
    audit.record(db, "live_authorization.granted", actor_type="USER", actor_user_id=user_id,
                 dealership_id=dealership_id, scope=scope, statement=statement)
    return auth


def revoke_live(db: Session, scope: str, *, user_id: int, dealership_id: int | None = None) -> int:
    stmt = select(LiveAuthorization).where(LiveAuthorization.scope == scope, LiveAuthorization.revoked_at.is_(None))
    if dealership_id is not None:
        stmt = stmt.where(LiveAuthorization.dealership_id == dealership_id)
    rows = list(db.scalars(stmt))
    for row in rows:
        row.revoked_at = utcnow()
        row.revoked_by_id = user_id
    audit.record(db, "live_authorization.revoked", actor_type="USER", actor_user_id=user_id,
                 dealership_id=dealership_id, scope=scope, count=len(rows))
    return len(rows)

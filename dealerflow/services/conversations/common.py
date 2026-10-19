"""Small adapters between the ported Phase 3 code and dealerflow's shared services."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...audit import audit
from ...db import utcnow
from ...models import AppSetting, User


def record(db: Session, action: str, *, client_id: int | None = None, actor_id: int | None = None,
           entity_type: str | None = None, entity_id: int | None = None, outcome: str = "OK", **details: Any) -> None:
    """Audit entry in the shared ``audit_log`` (Phase 3 called this ``audit.record``)."""
    data: dict[str, Any] = {k: v for k, v in details.items() if v is not None}
    if client_id is not None:
        data["client_id"] = client_id
    if outcome != "OK":
        data["outcome"] = outcome
    audit(db, action, actor_id=actor_id, entity_type=entity_type, entity_id=entity_id, data=data or None)


def local_now(tz: str) -> datetime:
    return utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def local_to_utc(local: datetime, tz: str) -> datetime:
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def utc_to_local(dt: datetime, tz: str) -> datetime:
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def dealership_users(db: Session, client_id: int) -> list[User]:
    """Active dealership (client) users of this client — the Phase 3 "members"."""
    return list(db.scalars(select(User).where(User.client_id == client_id, User.active.is_(True)).order_by(User.email)))


def assignable_users(db: Session, client_id: int) -> list[User]:
    """People a conversation may be assigned to: this dealership's users and active agency staff (rep and above)."""
    staff = db.scalars(select(User).where(User.client_id.is_(None), User.active.is_(True),
                                          User.role.in_(("rep", "manager", "admin"))).order_by(User.email))
    return dealership_users(db, client_id) + list(staff)


def get_flag(db: Session, key: str) -> bool:
    row = db.get(AppSetting, key)
    return bool(row and isinstance(row.value, dict) and row.value.get("on"))


def set_flag(db: Session, key: str, value: bool, *, actor_id: int | None, reason: str | None = None) -> None:
    row = db.get(AppSetting, key)
    payload = {"on": bool(value), "reason": reason, "at": utcnow().isoformat(timespec="seconds"), "by": actor_id}
    if row is None:
        db.add(AppSetting(key=key, value=payload))
    else:
        row.value = payload
    record(db, f"control.{key}.{'on' if value else 'off'}", actor_id=actor_id, reason=reason)

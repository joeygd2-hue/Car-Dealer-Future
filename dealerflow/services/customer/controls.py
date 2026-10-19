"""Platform-wide switches for customer messaging, stored in ``app_settings`` (shared by web and worker)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ... import audit
from ...db import utcnow
from ...models import AppSetting

GLOBAL_AUTOMATION_PAUSE = "customer.global_automation_pause"


def get_flag(db: Session, key: str) -> bool:
    row = db.get(AppSetting, key)
    return bool(row and isinstance(row.value, dict) and row.value.get("on"))


def set_flag(db: Session, key: str, value: bool, *, user_id: int | None, reason: str | None) -> None:
    row = db.get(AppSetting, key)
    payload = {"on": bool(value), "reason": reason, "at": utcnow().isoformat(timespec="seconds")}
    if row is None:
        db.add(AppSetting(key=key, value=payload, updated_by_id=user_id))
    else:
        row.value, row.updated_by_id = payload, user_id
    db.flush()
    audit.event(db, f"control.{key}.{'on' if value else 'off'}", actor_id=user_id, reason=reason)

"""Shared helpers for the conversation services."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ...audit import audit
from ...db import utcnow


def log(db: Session, action: str, *, client_id: int, entity_type: str, entity_id: int | None, actor_id: int | None = None,
        actor_type: str = "system", **details: Any) -> None:
    """Audit a conversation event. Callers pass ids, statuses and reasons only — never message text or contact details."""
    audit(db, action, actor_id=actor_id, entity_type=entity_type, entity_id=entity_id,
          data={"client_id": client_id, "actor_type": actor_type, **details})


def conversation_link(client_id: int, conversation_id: int) -> str:
    return f"/dealers/{client_id}/conversations/{conversation_id}"


def local_now(tz: str) -> datetime:
    return utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def local_to_utc(local: datetime, tz: str) -> datetime:
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def utc_to_local(dt: datetime, tz: str) -> datetime:
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).replace(tzinfo=None)

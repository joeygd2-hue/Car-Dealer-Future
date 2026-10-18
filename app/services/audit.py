"""Audit logging helper. Details are scrubbed of secrets before storage."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.core import AuditLog, User

_SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "authorization", "card", "cvv", "smtp_password"}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if any(s in k.lower() for s in _SENSITIVE_KEYS) else _scrub(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def record(
    db: Session,
    action: str,
    *,
    actor: User | None = None,
    client_id: int | None = None,
    entity: Any = None,
    entity_type: str | None = None,
    entity_id: Any = None,
    details: dict | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> AuditLog:
    if entity is not None:
        entity_type = entity_type or entity.__class__.__name__
        entity_id = entity_id if entity_id is not None else getattr(entity, "id", None)
        if client_id is None:
            client_id = getattr(entity, "client_id", None)
    row = AuditLog(
        action=action,
        actor_user_id=actor.id if actor else None,
        actor_label=actor.email if actor else "system",
        client_id=client_id,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=_scrub(details or {}),
        ip=ip,
        request_id=request_id,
    )
    db.add(row)
    return row

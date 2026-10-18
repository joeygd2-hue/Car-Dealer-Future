"""Audit trail helper. Call for every external action, state transition and admin change."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def record(
    db: Session,
    action: str,
    *,
    actor_type: str = "SYSTEM",
    actor_user_id: int | None = None,
    dealership_id: int | None = None,
    entity_type: str | None = None,
    entity_id: Any = None,
    outcome: str = "OK",
    external: bool = False,
    **details: Any,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        dealership_id=dealership_id,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        outcome=outcome,
        external=external,
        details=_jsonable(details),
    )
    db.add(entry)
    return entry


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def verify_integrity(db: Session, limit: int | None = None) -> dict:
    """Recompute every row's HMAC and look for id gaps (deleted rows). Returns a report for the admin UI."""
    from sqlalchemy import select

    from app.models.core import audit_entry_hmac

    stmt = select(AuditLog).order_by(AuditLog.id)
    if limit:
        stmt = stmt.limit(limit)
    tampered, gaps, unsealed, checked, prev = [], [], [], 0, None
    for row in db.scalars(stmt):
        checked += 1
        if row.entry_hmac is None:
            unsealed.append(row.id)
        elif row.entry_hmac != audit_entry_hmac(row):
            tampered.append(row.id)
        if prev is not None and row.id != prev + 1:
            gaps.append([prev + 1, row.id - 1])
        prev = row.id
    return {"checked": checked, "tampered_ids": tampered, "missing_id_ranges": gaps, "unsealed_ids": unsealed,
            "ok": not (tampered or gaps or unsealed)}

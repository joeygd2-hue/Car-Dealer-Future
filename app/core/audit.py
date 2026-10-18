from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(db: Session, *, action: str, entity_type: str, entity_id: str | None = None, client_id: str | None = None,
          actor_type: str = "system", actor_id: str | None = None, details: dict[str, Any] | None = None,
          cost_usd: float | None = None) -> None:
    db.add(AuditLog(client_id=client_id, actor_type=actor_type, actor_id=actor_id, action=action, entity_type=entity_type,
                    entity_id=entity_id, details=details or {}, cost_usd=cost_usd))


def audit_ctx(ctx, action: str, entity_type: str, entity_id: str | None = None, **details) -> None:
    audit(ctx.db, action=action, entity_type=entity_type, entity_id=entity_id, client_id=ctx.client_id,
          actor_type=ctx.actor.type, actor_id=ctx.actor.id, details=details)


@event.listens_for(Session, "before_flush")
def _audit_append_only(session, flush_context, instances):
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, AuditLog) and (obj in session.deleted or session.is_modified(obj)):
            raise PermissionError("audit_log is append-only")

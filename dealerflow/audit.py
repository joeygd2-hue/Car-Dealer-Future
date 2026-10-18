"""Audit trail and integration call log."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy.orm import Session

from .models import AuditLog, IntegrationLog

log = logging.getLogger("dealerflow.audit")


def audit(
    db: Session,
    action: str,
    *,
    actor_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    data: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    entry = AuditLog(actor_id=actor_id, action=action, entity_type=entity_type, entity_id=entity_id, data=data, ip=ip)
    db.add(entry)
    log.info("audit", extra={"action": action, "actor_id": actor_id, "entity": f"{entity_type}:{entity_id}"})
    return entry


def log_integration(
    db: Session,
    provider: str,
    action: str,
    status: str,
    *,
    detail: str = "",
    simulated: bool = False,
    duration_ms: int | None = None,
) -> IntegrationLog:
    row = IntegrationLog(
        provider=provider, action=action, status=status, detail=detail[:4000], simulated=simulated, duration_ms=duration_ms
    )
    db.add(row)
    level = logging.INFO if status == "ok" else logging.WARNING
    log.log(level, "integration", extra={"provider": provider, "action": action, "status": status, "simulated": simulated})
    return row


@contextmanager
def track_integration(db: Session, provider: str, action: str, *, simulated: bool = False) -> Iterator[dict]:
    """Log an external call; the caller may set ``ctx['detail']``. Errors are logged and re-raised."""
    from .integrations.errors import CredentialError, RateLimitedError

    ctx: dict[str, Any] = {"detail": ""}
    start = time.monotonic()
    try:
        yield ctx
    except CredentialError as exc:
        log_integration(db, provider, action, "auth_error", detail=str(exc), simulated=simulated,
                        duration_ms=int((time.monotonic() - start) * 1000))
        raise
    except RateLimitedError as exc:
        log_integration(db, provider, action, "rate_limited", detail=str(exc), simulated=simulated,
                        duration_ms=int((time.monotonic() - start) * 1000))
        raise
    except Exception as exc:
        log_integration(db, provider, action, "error", detail=f"{type(exc).__name__}: {exc}", simulated=simulated,
                        duration_ms=int((time.monotonic() - start) * 1000))
        raise
    else:
        log_integration(db, provider, action, "ok", detail=ctx.get("detail", ""), simulated=simulated,
                        duration_ms=int((time.monotonic() - start) * 1000))

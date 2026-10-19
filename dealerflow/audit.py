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


DISABLED_KEY = "integration_disabled:{}"


def integration_disabled(db: Session, provider: str) -> dict | None:
    from .models import AppSetting

    row = db.get(AppSetting, DISABLED_KEY.format(provider))
    return row.value if row else None


def set_integration_disabled(db: Session, provider: str, detail: str | None) -> None:
    """``detail`` disables the provider (after an auth failure); ``None`` re-enables it (after a passing check)."""
    from .db import utcnow
    from .models import AppSetting

    key = DISABLED_KEY.format(provider)
    row = db.get(AppSetting, key)
    if detail is None:
        if row is not None:
            db.delete(row)
            audit(db, "integration.enabled", data={"provider": provider})
        return
    if row is None:
        db.add(AppSetting(key=key, value={"at": utcnow().isoformat(), "detail": detail[:500]}))
        audit(db, "integration.disabled", data={"provider": provider, "detail": detail[:500]})


def _gate(db: Session, provider: str, simulated: bool) -> None:
    """Checked before every provider call, real or simulated."""
    from .integrations.errors import ExternalActionsHalted, IntegrationDisabled
    from .models import AgentState

    st = db.get(AgentState, 1)
    if st is not None and st.status == "emergency_stopped":
        raise ExternalActionsHalted("Emergency stop is active; no external actions are allowed")
    off = integration_disabled(db, provider)
    if off:
        raise IntegrationDisabled(f"{provider} is disabled after an authentication failure at {off['at']}; "
                                  "fix the credentials and run Integrations → Verify credentials")


def _record_failure(db: Session, provider: str, action: str, status: str, detail: str, simulated: bool,
                    duration_ms: int) -> None:
    log_integration(db, provider, action, status, detail=detail, simulated=simulated, duration_ms=duration_ms)
    if status == "auth_error":
        set_integration_disabled(db, provider, detail)
    # Kept on the session so a caller that rolls back (a failing job) can write it again: see replay_failures.
    db.info.setdefault("integration_failures", []).append(
        dict(provider=provider, action=action, status=status, detail=detail, simulated=simulated, duration_ms=duration_ms))


def replay_failures(db: Session) -> None:
    """Re-record failures that a rollback discarded, so they stay traceable (and still disable providers)."""
    pending = db.info.pop("integration_failures", [])
    for f in pending:
        log_integration(db, f["provider"], f["action"], f["status"], detail=f["detail"], simulated=f["simulated"],
                        duration_ms=f["duration_ms"])
        if f["status"] == "auth_error":
            set_integration_disabled(db, f["provider"], f["detail"])


def clear_failures(db: Session) -> None:
    db.info.pop("integration_failures", None)


@contextmanager
def track_integration(db: Session, provider: str, action: str, *, simulated: bool = False) -> Iterator[dict]:
    """Gate, then log, every external call. The caller may set ``ctx['detail']``. Errors are logged and re-raised."""
    from .integrations.errors import CredentialError, ExternalActionsHalted, IntegrationDisabled, RateLimitedError

    try:
        _gate(db, provider, simulated)
    except (ExternalActionsHalted, IntegrationDisabled) as exc:
        _record_failure(db, provider, action, "blocked", str(exc), simulated, 0)
        raise
    ctx: dict[str, Any] = {"detail": ""}
    start = time.monotonic()

    def ms() -> int:
        return int((time.monotonic() - start) * 1000)

    try:
        yield ctx
    except CredentialError as exc:
        _record_failure(db, provider, action, "auth_error", str(exc), simulated, ms())
        raise
    except RateLimitedError as exc:
        _record_failure(db, provider, action, "rate_limited", str(exc), simulated, ms())
        raise
    except Exception as exc:
        _record_failure(db, provider, action, "error", f"{type(exc).__name__}: {exc}", simulated, ms())
        raise
    else:
        log_integration(db, provider, action, "ok", detail=ctx.get("detail", ""), simulated=simulated, duration_ms=ms())


def record(db: Session, action: str, *, actor=None, entity=None, client_id: int | None = None,
           details: dict[str, Any] | None = None, ip: str | None = None) -> AuditLog:
    """Convenience wrapper over ``audit`` for Phase 2-4 code: derives entity type/id and client from ``entity``.

    Values whose key looks secret are redacted before storage.
    """
    data = _scrub(dict(details or {}))
    if client_id is None and entity is not None:
        client_id = getattr(entity, "client_id", None)
    if client_id is not None:
        data["client_id"] = client_id
    return audit(db, action, actor_id=getattr(actor, "id", None),
                 entity_type=type(entity).__name__ if entity is not None else None,
                 entity_id=getattr(entity, "id", None) if entity is not None else None, data=data or None, ip=ip)


def event(db: Session, action: str, *, actor_id: int | None = None, client_id: int | None = None,
          entity_type: str | None = None, entity_id: int | None = None, **details: Any) -> AuditLog:
    """Keyword-style audit entry used by the Phase 3 customer-messaging code (secret-looking keys redacted)."""
    data = _scrub({k: v for k, v in details.items() if v is not None})
    if client_id is not None:
        data["client_id"] = client_id
    return audit(db, action, actor_id=actor_id, entity_type=entity_type, entity_id=entity_id, data=data or None)


_SENSITIVE_KEYS = ("password", "token", "secret", "api_key", "authorization", "card", "cvv")


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[redacted]" if any(s in str(k).lower() for s in _SENSITIVE_KEYS) else _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

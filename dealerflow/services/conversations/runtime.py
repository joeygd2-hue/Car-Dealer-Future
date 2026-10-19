"""How the customer-response engine (ported from Phase 3) uses dealerflow's shared services.

One audit log, one job queue, one emergency stop:

- ``record`` writes dealerflow ``audit_logs`` rows; Phase 3's ``client_id``/``actor_type``/``outcome``/
  ``external`` fields are kept in ``data``.
- The platform **emergency stop** is the agent's ``emergency_stopped`` state (Agent page), the same switch
  that halts every outreach provider call. It blocks every customer message and staff email too.
- The **global automation pause** (customer auto-replies only) is an ``app_settings`` flag.
- Jobs run on dealerflow's queue. ``RetryLater`` re-runs a job without using an attempt; ``PermanentJobError``
  fails it immediately; ``TransientSendError`` is retried with backoff.
- A customer channel is **live** only when ``DF_CUSTOMER_MESSAGING_LIVE_ENABLED=true`` AND the client's
  ``customer_messaging`` connection for that channel is ``connected`` (which Phase 4 only allows with a
  written authorization note). Otherwise every send is SIMULATED.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...audit import audit
from ...config import get_settings
from ...db import utcnow
from ...integrations.errors import IntegrationError
from ...jobs import STALE_LOCK, DeferJob, enqueue as _enqueue, handler as _handler, on_failure
from ...models import AgentState, AppSetting, IntegrationConnection

GLOBAL_AUTOMATION_PAUSE = "automation.global_pause"
CONNECTION_KIND = "customer_messaging"


# --- audit ------------------------------------------------------------------------------------------


def record(db: Session, action: str, *, actor_type: str = "SYSTEM", actor_user_id: int | None = None,
           client_id: int | None = None, entity_type: str | None = None, entity_id: Any = None, outcome: str = "OK",
           external: bool = False, **details: Any):
    data: dict[str, Any] = {"client_id": client_id, "actor_type": actor_type, "outcome": outcome, "external": external}
    data.update(_jsonable(details))
    try:
        eid = int(entity_id) if entity_id is not None else None
    except (TypeError, ValueError):
        eid, data["entity_ref"] = None, str(entity_id)
    return audit(db, action, actor_id=actor_user_id, entity_type=entity_type, entity_id=eid, data=data)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# --- switches ---------------------------------------------------------------------------------------


def emergency_stopped(db: Session) -> bool:
    st = db.get(AgentState, 1)
    return st is not None and st.status == "emergency_stopped"


def get_flag(db: Session, key: str) -> bool:
    row = db.get(AppSetting, f"control:{key}")
    return bool(row and (row.value or {}).get("on"))


def set_flag(db: Session, key: str, value: bool, *, user_id: int | None, reason: str | None) -> None:
    row = db.get(AppSetting, f"control:{key}")
    payload = {"on": value, "reason": reason, "at": utcnow().isoformat()}
    if row is None:
        db.add(AppSetting(key=f"control:{key}", value=payload, updated_by_id=user_id))
    else:
        row.value, row.updated_by_id = payload, user_id
    record(db, f"control.{key}.{'on' if value else 'off'}", actor_type="USER" if user_id else "SYSTEM",
           actor_user_id=user_id, reason=reason)


def channel_connection(db: Session, client_id: int, channel: str) -> IntegrationConnection | None:
    return db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.client_id == client_id, IntegrationConnection.kind == CONNECTION_KIND,
        IntegrationConnection.provider == channel))


def live_authorized(db: Session, client_id: int | None, channel: str) -> bool:
    if not get_settings().customer_messaging_live_enabled or client_id is None:
        return False
    conn = channel_connection(db, client_id, channel)
    return conn is not None and conn.status == "connected"


# --- jobs -------------------------------------------------------------------------------------------

WORKER_LEASE_SECONDS = int(STALE_LOCK.total_seconds())


class PermanentJobError(RuntimeError):
    """Do not retry (bad input, forbidden action, permanently rejected by provider)."""


class TransientSendError(IntegrationError):
    """Definitely not delivered; safe to retry with backoff."""

    retryable = True


class RetryLater(DeferJob):
    """Re-run later without counting an attempt. ``persist`` records why (it runs after the rollback)."""

    def __init__(self, delay_seconds: float, reason: str = "", persist: Callable[[Session], None] | None = None):
        super().__init__(utcnow() + timedelta(seconds=max(1.0, float(delay_seconds))), reason, persist=persist)


def enqueue(db: Session, kind: str, payload: dict | None, *, key: str, run_at: datetime | None = None,
            max_attempts: int = 5):
    delay = (run_at - utcnow()) if run_at and run_at > utcnow() else None
    job, _ = _enqueue(db, kind, key, payload or {}, max_attempts=max_attempts, delay=delay)
    return job


def handler(kind: str, *, on_dead: Callable[[Session, dict, str], None] | None = None):
    def deco(fn):
        _handler(kind)(fn)
        if on_dead is not None:
            on_failure(kind)(on_dead)
        return fn

    return deco


def payload_get(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise PermanentJobError(f"job payload missing '{key}'")
    return payload[key]


def schedule(db: Session, now: datetime | None = None) -> int:
    """Called by the worker every loop: the hourly digest check and reminder recovery sweep (idempotent per hour)."""
    hour = (now or utcnow()).strftime("%Y-%m-%dT%H")
    created = 0
    for kind in ("notification.daily_digests", "appointments.dispatch_reminders"):
        _, c = _enqueue(db, kind, f"{kind}:{hour}", {}, max_attempts=2)
        created += int(c)
    return created

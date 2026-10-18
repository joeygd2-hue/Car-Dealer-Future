"""Background worker: heartbeat, agent tick scheduling and job execution.

Run with ``python -m dealerflow worker``. Safe to restart at any time: jobs are claimed
atomically, stale locks are released, ticks are idempotent per time bucket.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time

from .db import session_scope, utcnow
from .jobs import enqueue, run_pending

log = logging.getLogger("dealerflow.worker")
_stop = False


def _handle(sig, _frame):  # pragma: no cover - signal path
    global _stop
    _stop = True
    log.info("worker stopping", extra={"signal": sig})


def schedule_tick(now=None) -> bool:
    """Enqueue the agent tick for the current time bucket. Returns True if newly created."""
    from .services import agent

    now = now or utcnow()
    with session_scope() as db:
        cfg = agent.config(db)
        minutes = max(1, int(cfg["tick_minutes"]))
        bucket = now.replace(second=0, microsecond=0)
        bucket = bucket.replace(minute=bucket.minute - bucket.minute % minutes) if minutes < 60 else bucket.replace(minute=0)
        _, created = enqueue(db, "agent_tick", f"agent_tick:{bucket.isoformat()}", {}, max_attempts=1)
        schedule_daily(db, now)
        return created


def schedule_daily(db, now) -> None:
    """Once-per-day jobs, keyed by local date so restarts never duplicate them."""
    from datetime import timezone
    from zoneinfo import ZoneInfo

    from .config import get_settings

    s = get_settings()
    local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(s.timezone))
    if local.hour >= 7 and s.notify_email:
        day = local.date().isoformat()
        enqueue(db, "daily_digest", f"daily_digest:{day}", {"day": day}, max_attempts=3)


def run_once(worker_id: str = "once") -> dict:
    from . import handlers  # noqa: F401  (registers job handlers)
    from .services import agent

    with session_scope() as db:
        agent.heartbeat(db, worker_id)
    schedule_tick()
    return run_pending()


def main(poll_seconds: float = 10.0) -> None:
    from . import handlers  # noqa: F401
    from .services import agent

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    with session_scope() as db:
        n = agent.recover_after_restart(db)
    log.info("worker started", extra={"worker_id": worker_id, "recovered_runs": n})
    while not _stop:
        try:
            counts = run_once(worker_id)
            if counts:
                log.info("jobs ran", extra={"counts": counts})
        except Exception:  # noqa: BLE001 - keep the worker alive; errors are logged
            log.exception("worker loop error")
        for _ in range(int(poll_seconds * 10)):
            if _stop:
                break
            time.sleep(0.1)
    log.info("worker stopped")

"""Database-backed background jobs.

- ``enqueue`` is idempotent: the same ``key`` never creates a second job.
- A job is claimed by flipping status pending -> running in one UPDATE, so two workers cannot
  both run it.
- Retryable failures back off exponentially; CredentialError / PermanentError fail immediately.
- Handlers themselves must be idempotent (e.g. sending checks the message status first).
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import session_scope, utcnow
from .audit import clear_failures, replay_failures
from .integrations.errors import ExternalActionsHalted, IntegrationError, RateLimitedError
from .models import Job

log = logging.getLogger("dealerflow.jobs")

Handler = Callable[[Session, dict[str, Any]], dict[str, Any] | None]
HANDLERS: dict[str, Handler] = {}
FAILURE_HOOKS: dict[str, Callable[[Session, dict[str, Any], str], None]] = {}


class DeferJob(Exception):
    """Raised by a handler to postpone the job (e.g. daily cap reached) without using an attempt."""

    def __init__(self, until: datetime, reason: str, persist: Callable[[Session], None] | None = None):
        super().__init__(reason)
        self.until = until
        self.persist = persist  # optional: record why the work is waiting (runs after the rollback)
STALE_LOCK = timedelta(minutes=15)


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


def on_failure(kind: str):
    """Register a hook called (in the same transaction) when a job of ``kind`` fails permanently."""

    def deco(fn):
        FAILURE_HOOKS[kind] = fn
        return fn

    return deco


def enqueue(db: Session, kind: str, key: str, payload: dict[str, Any] | None = None, *,
            max_attempts: int = 5, delay: timedelta | None = None) -> tuple[Job, bool]:
    """Return (job, created). Re-enqueueing an existing key returns the existing job."""
    existing = db.scalar(select(Job).where(Job.key == key))
    if existing:
        return existing, False
    job = Job(kind=kind, key=key, payload=payload or {}, max_attempts=max_attempts,
              run_after=utcnow() + (delay or timedelta()))
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(Job).where(Job.key == key))
        assert existing is not None
        return existing, False
    return job, True


def _claim(db: Session, job_id: int) -> bool:
    now = utcnow()
    res = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "pending")
        .values(status="running", locked_at=now, attempts=Job.attempts + 1)
    )
    db.commit()
    return res.rowcount == 1


def run_job(job_id: int) -> str:
    """Run one job in its own session. Returns final status."""
    with session_scope() as db:
        if not _claim(db, job_id):
            return "skipped"
        job = db.get(Job, job_id)
        assert job is not None
        fn = HANDLERS.get(job.kind)
        try:
            if fn is None:
                raise RuntimeError(f"no handler for job kind {job.kind}")
            result = fn(db, dict(job.payload or {}))
            job.status, job.result, job.finished_at, job.last_error = "done", result, utcnow(), None
            db.commit()
            clear_failures(db)
        except (DeferJob, ExternalActionsHalted) as exc:
            db.rollback()
            replay_failures(db)
            until = exc.until if isinstance(exc, DeferJob) else utcnow() + timedelta(minutes=10)
            job = db.get(Job, job_id)
            assert job is not None
            job.status, job.run_after, job.attempts = "pending", until, max(0, job.attempts - 1)
            job.last_error = f"deferred: {exc}"
            if isinstance(exc, DeferJob) and exc.persist is not None:
                exc.persist(db)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
            db.rollback()
            replay_failures(db)
            job = db.get(Job, job_id)
            assert job is not None
            job.last_error = f"{type(exc).__name__}: {exc}"[:4000]
            retryable = not isinstance(exc, IntegrationError) or exc.retryable
            if isinstance(exc, (KeyError, ValueError, RuntimeError)) and not isinstance(exc, IntegrationError):
                retryable = False
            if retryable and job.attempts < job.max_attempts:
                backoff = 60 * 2 ** (job.attempts - 1)
                if isinstance(exc, RateLimitedError) and exc.retry_after:
                    backoff = max(backoff, exc.retry_after)
                job.status, job.run_after = "pending", utcnow() + timedelta(seconds=backoff)
            else:
                job.status, job.finished_at = "failed", utcnow()
                hook = FAILURE_HOOKS.get(job.kind)
                if hook:
                    try:
                        hook(db, dict(job.payload or {}), job.last_error)
                    except Exception:  # noqa: BLE001
                        log.exception("failure hook error", extra={"job_id": job_id})
            log.warning("job failed", extra={"job_id": job_id, "kind": job.kind, "status": job.status,
                                              "error": job.last_error, "tb": traceback.format_exc(limit=3)})
            db.commit()
        return job.status


def run_pending(limit: int = 50) -> dict[str, int]:
    """Run due jobs once. Also releases jobs whose worker died mid-run."""
    counts: dict[str, int] = {}
    with session_scope() as db:
        db.execute(
            update(Job)
            .where(Job.status == "running", Job.locked_at < utcnow() - STALE_LOCK)
            .values(status="pending", locked_at=None)
        )
        ids = list(db.scalars(
            select(Job.id).where(Job.status == "pending", Job.run_after <= utcnow()).order_by(Job.run_after).limit(limit)
        ))
    for jid in ids:
        status = run_job(jid)
        counts[status] = counts.get(status, 0) + 1
    return counts

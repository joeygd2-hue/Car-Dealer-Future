"""Durable, database-backed job queue shared by every phase.

Guarantees
- Persisted: jobs live in the ``jobs`` table and survive process restarts.
- At-least-once execution with leases: a job whose worker died is reclaimed when its lease expires.
  Handlers must therefore be idempotent (use idempotency keys / status checks before external calls).
- Idempotent enqueue: ``idempotency_key`` is unique; re-enqueueing returns the existing job.
- Bounded retries with exponential backoff; jobs that exhaust ``max_attempts`` become ``DEAD``.
- ``RetryLater`` re-schedules without consuming an attempt (rate limits, send windows).
- Works on SQLite (dev/test) and PostgreSQL (prod) using conditional UPDATE claims.
"""

from __future__ import annotations

import logging
import random
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal, utcnow
from app.models import Job, RecurringTask
from app.services import audit

log = logging.getLogger(__name__)

QUEUED, RUNNING, SUCCEEDED, DEAD, CANCELLED = "QUEUED", "RUNNING", "SUCCEEDED", "DEAD", "CANCELLED"


class PermanentJobError(Exception):
    """Do not retry (bad input, forbidden action, permanently rejected by provider)."""


class RetryLater(Exception):  # noqa: N818 - control-flow signal, not an error
    """Re-run later without counting an attempt (e.g. outside send window, rate limited)."""

    def __init__(self, delay_seconds: float, reason: str = "", persist: Callable[[Session], None] | None = None):
        super().__init__(reason)
        self.delay_seconds = max(1.0, float(delay_seconds))
        self.reason = reason
        self.persist = persist  # optional: record why the work is waiting (runs after rollback)


Handler = Callable[[Session, dict, Job], dict | None]
DeadHook = Callable[[Session, dict, Job], None]


@dataclass
class _Registration:
    fn: Handler
    on_dead: DeadHook | None = None


_REGISTRY: dict[str, _Registration] = {}


def handler(job_type: str, *, on_dead: DeadHook | None = None) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _REGISTRY[job_type] = _Registration(fn, on_dead)
        return fn

    return deco


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


def enqueue(
    db: Session,
    job_type: str,
    payload: dict | None = None,
    *,
    idempotency_key: str | None = None,
    run_at: datetime | None = None,
    max_attempts: int = 5,
    priority: int = 100,
) -> Job:
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing:
            return existing
    job = Job(
        type=job_type,
        payload=payload or {},
        idempotency_key=idempotency_key,
        run_at=run_at or utcnow(),
        max_attempts=max_attempts,
        priority=priority,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing
    return job


def backoff_seconds(attempt: int, *, base: float = 5.0, cap: float = 3600.0, jitter: bool = True) -> float:
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    if jitter:
        delay = delay * (0.8 + random.random() * 0.4)
    return delay


def _claimable(now: datetime):
    return or_(
        and_(Job.status == QUEUED, Job.run_at <= now),
        and_(Job.status == RUNNING, Job.locked_until < now),  # lease expired → recovery
    )


def claim_next(db: Session, worker_id: str, lease_seconds: int = 120, types: list[str] | None = None) -> int | None:
    now = utcnow()
    stmt = select(Job.id).where(_claimable(now))
    if types:
        stmt = stmt.where(Job.type.in_(types))
    candidate_ids = list(db.scalars(stmt.order_by(Job.priority, Job.run_at, Job.id).limit(20)))
    for job_id in candidate_ids:
        result = db.execute(
            update(Job)
            .where(Job.id == job_id, _claimable(now))
            .values(
                status=RUNNING,
                locked_by=worker_id,
                locked_until=now + timedelta(seconds=lease_seconds),
                attempts=Job.attempts + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if result.rowcount == 1:
            return job_id
    return None


def run_job(job_id: int, worker_id: str) -> str:
    """Execute one claimed job in its own session. Returns the resulting job status."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status != RUNNING or job.locked_by != worker_id:
            return "SKIPPED"
        reg = _REGISTRY.get(job.type)
        if reg is None:
            return _finish_dead(db, job, f"No handler registered for job type {job.type!r}", None)
        if job.attempts > job.max_attempts:
            return _finish_dead(db, job, job.last_error or "Exceeded max attempts (lease expired repeatedly)", reg)
        try:
            result = reg.fn(db, dict(job.payload or {}), job)
            db.flush()
        except RetryLater as exc:
            db.rollback()
            if exc.persist:
                exc.persist(db)
            job = db.get(Job, job_id)
            job.status = QUEUED
            job.attempts = max(0, job.attempts - 1)
            job.run_at = utcnow() + timedelta(seconds=exc.delay_seconds)
            job.last_error = f"deferred: {exc.reason}" if exc.reason else "deferred"
            job.locked_by = job.locked_until = None
            db.commit()
            return QUEUED
        except PermanentJobError as exc:
            db.rollback()
            return _finish_dead(db, db.get(Job, job_id), f"permanent: {exc}", reg)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried
            db.rollback()
            job = db.get(Job, job_id)
            err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
            log.warning("job %s (%s) attempt %s failed: %s", job.id, job.type, job.attempts, exc)
            if job.attempts >= job.max_attempts:
                return _finish_dead(db, job, err, reg)
            job.status = QUEUED
            job.last_error = err[:4000]
            job.run_at = utcnow() + timedelta(seconds=backoff_seconds(job.attempts))
            job.locked_by = job.locked_until = None
            db.commit()
            return QUEUED
        job.status = SUCCEEDED
        job.result = result or {}
        job.finished_at = utcnow()
        job.locked_by = job.locked_until = None
        db.commit()
        return SUCCEEDED
    finally:
        db.close()


def _finish_dead(db: Session, job: Job, error: str, reg: _Registration | None) -> str:
    job.status = DEAD
    job.last_error = error[:4000]
    job.finished_at = utcnow()
    job.locked_by = job.locked_until = None
    audit.record(db, "job.dead", actor_type="WORKER", entity_type="job", entity_id=job.id,
                 outcome="FAILED", job_type=job.type, error=error[:500])
    db.commit()
    if reg and reg.on_dead:
        try:
            reg.on_dead(db, dict(job.payload or {}), job)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("on_dead hook failed for job %s", job.id)
    return DEAD


def retry_dead(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job and job.status == DEAD:
        job.status = QUEUED
        job.attempts = 0
        job.run_at = utcnow()
        job.last_error = None
    return job


def cancel_jobs(db: Session, job_types: list[str]) -> int:
    res = db.execute(
        update(Job)
        .where(Job.type.in_(job_types), Job.status == QUEUED)
        .values(status=CANCELLED, finished_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    return res.rowcount or 0


def queue_counts(db: Session, prefix: str | None = None) -> dict[str, int]:
    stmt = select(Job.status, func.count()).group_by(Job.status)
    if prefix:
        stmt = stmt.where(Job.type.startswith(prefix))
    return {status: count for status, count in db.execute(stmt)}


# ---------------------------------------------------------------- recurring scheduler


def ensure_recurring(db: Session, name: str, job_type: str, interval_seconds: int, payload: dict | None = None) -> RecurringTask:
    task = db.scalar(select(RecurringTask).where(RecurringTask.name == name))
    if task is None:
        task = RecurringTask(name=name, job_type=job_type, interval_seconds=interval_seconds, payload=payload or {})
        db.add(task)
    else:
        task.job_type = job_type
        task.interval_seconds = interval_seconds
        task.payload = payload or {}
    return task


def scheduler_tick(db: Session) -> list[str]:
    """Enqueue due recurring jobs exactly once per slot, even with several schedulers running."""
    now = utcnow()
    enqueued: list[str] = []
    due = list(db.scalars(select(RecurringTask).where(RecurringTask.enabled.is_(True), RecurringTask.next_run_at <= now)))
    for task in due:
        slot = task.next_run_at
        res = db.execute(
            update(RecurringTask)
            .where(RecurringTask.id == task.id, RecurringTask.next_run_at == slot)
            .values(next_run_at=now + timedelta(seconds=task.interval_seconds), last_enqueued_at=now)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 1:
            enqueue(db, task.job_type, dict(task.payload or {}),
                    idempotency_key=f"recurring:{task.name}:{int(slot.timestamp())}", max_attempts=3)
            enqueued.append(task.name)
    db.commit()
    return enqueued


def payload_get(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise PermanentJobError(f"payload missing {key!r}")
    return payload[key]

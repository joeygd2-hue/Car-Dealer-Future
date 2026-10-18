"""Durable DB-backed job queue with leases, retries, idempotency and a scheduler.

Guarantees:
* At-least-once execution. Handlers must be idempotent (they use idempotency
  keys on the side effects they create).
* A job is claimed by atomically flipping `queued -> running` and setting a
  lease. If the worker dies, the lease expires and `recover_expired_leases()`
  requeues it (counts as an attempt).
* Failures retry with exponential backoff up to `max_attempts`, then become
  `dead` and raise an alert + support ticket where a client is involved.
* Scheduled tasks enqueue one job per due slot, keyed by `name:slot` so two
  schedulers can never enqueue the same slot twice.
"""
from __future__ import annotations

import logging
import socket
import os
import traceback
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.base import utcnow
from app.models.core import Job, ScheduledTask, WorkerHeartbeat

log = logging.getLogger(__name__)

Handler = Callable[[Session, Job], dict | None]
_HANDLERS: dict[str, Handler] = {}


class PermanentJobError(Exception):
    """Raise from a handler to fail a job without further retries."""


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn

    return deco


def registered_kinds() -> list[str]:
    return sorted(_HANDLERS)


def enqueue(
    db: Session,
    kind: str,
    payload: dict | None = None,
    *,
    idempotency_key: str | None = None,
    run_after=None,
    max_attempts: int = 5,
    priority: int = 100,
    client_id: int | None = None,
) -> Job:
    """Enqueue a job. With an idempotency key, returns the existing job instead of a duplicate."""
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing:
            return existing
    job = Job(
        kind=kind,
        payload=payload or {},
        idempotency_key=idempotency_key,
        run_after=run_after or utcnow(),
        max_attempts=max_attempts,
        priority=priority,
        client_id=client_id,
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        assert existing is not None
        return existing
    return job


def backoff_seconds(attempt: int) -> int:
    return min(3600, 30 * (2 ** max(0, attempt - 1)))


def recover_expired_leases(db: Session) -> int:
    """Requeue jobs whose worker lease expired (worker crashed or was restarted)."""
    now = utcnow()
    stuck = db.scalars(select(Job).where(Job.status == "running", Job.lease_expires_at < now)).all()
    for job in stuck:
        job.last_error = f"Lease expired (worker {job.locked_by} presumed dead)"
        job.locked_by = None
        job.lease_expires_at = None
        if job.attempts >= job.max_attempts:
            job.status = "dead"
            job.finished_at = now
        else:
            job.status = "queued"
            job.run_after = now
    db.commit()
    return len(stuck)


def claim_next(db: Session, worker_id: str) -> Job | None:
    """Atomically claim the next due job. Safe with concurrent workers."""
    now = utcnow()
    lease = timedelta(seconds=get_settings().job_lease_seconds)
    for _ in range(5):
        candidate = db.scalar(
            select(Job.id)
            .where(Job.status == "queued", Job.run_after <= now)
            .order_by(Job.priority, Job.run_after, Job.id)
            .limit(1)
        )
        if candidate is None:
            return None
        res = db.execute(
            update(Job)
            .where(Job.id == candidate, Job.status == "queued")
            .values(
                status="running",
                locked_by=worker_id,
                lease_expires_at=now + lease,
                attempts=Job.attempts + 1,
                started_at=now,
            )
        )
        db.commit()
        if res.rowcount == 1:
            return db.get(Job, candidate)
    return None


def run_job(db: Session, job: Job) -> None:
    fn = _HANDLERS.get(job.kind)
    now = utcnow()
    if fn is None:
        job.status = "dead"
        job.last_error = f"No handler registered for job kind '{job.kind}'"
        job.finished_at = now
        db.commit()
        return
    try:
        result = fn(db, job)
        db.flush()
        job.status = "succeeded"
        job.result = result or {}
        job.last_error = None
    except PermanentJobError as exc:
        db.rollback()
        job = db.get(Job, job.id)
        job.status = "dead"
        job.last_error = str(exc)[:4000]
        _on_dead(db, job)
    except Exception as exc:  # noqa: BLE001 - we record and retry every failure
        db.rollback()
        job = db.get(Job, job.id)
        job.last_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"[:4000]
        if job.attempts >= job.max_attempts:
            job.status = "dead"
            _on_dead(db, job)
        else:
            job.status = "queued"
            job.run_after = utcnow() + timedelta(seconds=backoff_seconds(job.attempts))
        log.warning("Job %s (%s) failed attempt %s: %s", job.id, job.kind, job.attempts, exc)
    finally:
        job.locked_by = None
        job.lease_expires_at = None
        if job.status in ("succeeded", "dead"):
            job.finished_at = utcnow()
        db.commit()


def _on_dead(db: Session, job: Job) -> None:
    from app.services import alerts

    alerts.raise_alert(
        db,
        kind="job_dead",
        message=f"Job #{job.id} ({job.kind}) failed permanently: {(job.last_error or '')[:300]}",
        severity="error",
        dedupe_key=f"job_dead:{job.id}",
    )
    if job.client_id:
        from app.services.tickets import open_system_ticket

        open_system_ticket(
            db,
            client_id=job.client_id,
            kind="integration_failure",
            title=f"Background job failed: {job.kind}",
            description=(job.last_error or "")[:2000],
            dedupe_key=f"job_dead:{job.kind}:{job.client_id}",
        )


def schedule_due_tasks(db: Session) -> int:
    """Enqueue a job for every scheduled task whose slot is due."""
    now = utcnow()
    count = 0
    for task in db.scalars(select(ScheduledTask).where(ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at <= now)):
        slot = int(task.next_run_at.timestamp()) // max(1, task.interval_seconds)
        enqueue(db, task.job_kind, dict(task.payload), idempotency_key=f"sched:{task.name}:{slot}", max_attempts=3)
        task.last_enqueued_at = now
        # Skip missed slots after downtime rather than enqueueing a backlog.
        nxt = task.next_run_at + timedelta(seconds=task.interval_seconds)
        if nxt <= now:
            nxt = now + timedelta(seconds=task.interval_seconds)
        task.next_run_at = nxt
        count += 1
    db.commit()
    return count


def ensure_scheduled_task(db: Session, name: str, job_kind: str, interval_seconds: int, payload: dict | None = None) -> None:
    task = db.scalar(select(ScheduledTask).where(ScheduledTask.name == name))
    if task is None:
        db.add(ScheduledTask(name=name, job_kind=job_kind, interval_seconds=interval_seconds, payload=payload or {}))
        db.commit()


def heartbeat(db: Session, worker_id: str, processed: int = 0) -> None:
    hb = db.get(WorkerHeartbeat, worker_id)
    if hb is None:
        hb = WorkerHeartbeat(worker_id=worker_id, jobs_processed=0)
        db.add(hb)
    hb.last_seen_at = utcnow()
    hb.jobs_processed += processed
    db.commit()


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_pending(db: Session, worker_id: str = "inline", limit: int = 100) -> int:
    """Run due jobs synchronously (used by tests and the `run-jobs` CLI)."""
    recover_expired_leases(db)
    done = 0
    while done < limit:
        job = claim_next(db, worker_id)
        if job is None:
            break
        run_job(db, job)
        done += 1
    return done


def queue_stats(db: Session) -> dict[str, Any]:
    from sqlalchemy import func

    rows = db.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    return {status: n for status, n in rows}

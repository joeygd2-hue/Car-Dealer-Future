"""Durable lease-based job queue on the shared database.

- ``enqueue`` is idempotent when given ``idempotency_key``.
- ``claim_next`` atomically leases one ready job (UPDATE ... WHERE status='queued'
  guards against two workers claiming the same job).
- Failures retry with exponential backoff + jitter up to ``max_attempts``, then
  become ``dead``. Non-retryable failures become ``failed`` immediately.
- ``recover_expired_leases`` requeues jobs whose worker died, so restarts resume.
- ``materialize_schedules`` turns recurring schedules into jobs; the slot time is
  part of the idempotency key so a schedule never double-fires.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import Job, JobSchedule, SystemFlag


def enqueue(db: Session, type: str, *, payload: dict[str, Any] | None = None, queue: str = "default",
            client_id: str | None = None, run_at: datetime | None = None, priority: int = 100, max_attempts: int = 5,
            idempotency_key: str | None = None) -> tuple[Job, bool]:
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing:
            return existing, False
    job = Job(type=type, queue=queue, client_id=client_id, payload=payload or {}, run_at=run_at or utcnow(),
              priority=priority, max_attempts=max_attempts, idempotency_key=idempotency_key, status="queued")
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        return db.scalar(select(Job).where(Job.idempotency_key == idempotency_key)), False
    return job, True


def claim_next(db: Session, worker_id: str, *, queues: list[str] | None = None, lease_seconds: int = 300,
               now: datetime | None = None) -> Job | None:
    now = now or utcnow()
    for _ in range(5):
        q = select(Job.id).where(Job.status == "queued", Job.run_at <= now)
        if queues:
            q = q.where(Job.queue.in_(queues))
        job_id = db.scalar(q.order_by(Job.priority, Job.run_at).limit(1))
        if not job_id:
            return None
        res = db.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(
            status="running", locked_by=worker_id, locked_until=now + timedelta(seconds=lease_seconds),
            attempts=Job.attempts + 1, updated_at=now))
        db.commit()
        if res.rowcount == 1:
            job = db.get(Job, job_id)
            db.refresh(job)
            return job
    return None


def extend_lease(db: Session, job_id: str, worker_id: str, lease_seconds: int = 300) -> bool:
    res = db.execute(update(Job).where(Job.id == job_id, Job.locked_by == worker_id, Job.status == "running")
                     .values(locked_until=utcnow() + timedelta(seconds=lease_seconds)))
    db.commit()
    return res.rowcount == 1


def complete_job(db: Session, job: Job, result: dict[str, Any] | None = None) -> None:
    job.status, job.result, job.locked_by, job.locked_until = "succeeded", result or {}, None, None
    job.last_error, job.finished_at = None, utcnow()
    db.commit()


def backoff_seconds(attempt: int, base: int = 30, cap: int = 3600, rnd: random.Random | None = None) -> int:
    exp = min(cap, base * 2 ** max(0, attempt - 1))
    return round(exp * (0.8 + (rnd or random).random() * 0.4))


def fail_job(db: Session, job: Job, error: Exception | str, *, retryable: bool = True, now: datetime | None = None) -> str:
    now = now or utcnow()
    db.refresh(job)
    job.last_error = str(error)[:2000]
    job.locked_by = job.locked_until = None
    if retryable and job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_at = now + timedelta(seconds=backoff_seconds(job.attempts))
        outcome = "retry_scheduled"
    else:
        job.status = "dead" if retryable else "failed"
        job.finished_at = now
        outcome = job.status
    db.commit()
    return outcome


def cancel_jobs(db: Session, *, type_prefix: str | None = None, queue: str | None = None, client_id: str | None = None) -> int:
    q = update(Job).where(Job.status == "queued")
    if type_prefix:
        q = q.where(Job.type.startswith(type_prefix))
    if queue:
        q = q.where(Job.queue == queue)
    if client_id:
        q = q.where(Job.client_id == client_id)
    n = db.execute(q.values(status="cancelled", finished_at=utcnow())).rowcount
    db.commit()
    return n


def recover_expired_leases(db: Session, *, now: datetime | None = None) -> int:
    now = now or utcnow()
    expired = db.scalars(select(Job).where(Job.status == "running", Job.locked_until < now)).all()
    for job in expired:
        fail_job(db, job, f"Lease expired (worker {job.locked_by} stopped or exceeded its lease)", now=now)
    return len(expired)


def upsert_schedule(db: Session, key: str, type: str, interval_seconds: int, *, payload: dict | None = None,
                    queue: str = "default", client_id: str | None = None, enabled: bool = True,
                    first_run_at: datetime | None = None) -> JobSchedule:
    if interval_seconds < 30:
        raise ValueError("interval_seconds must be >= 30")
    s = db.get(JobSchedule, key)
    if not s:
        s = JobSchedule(key=key, next_run_at=first_run_at or utcnow())
        db.add(s)
    s.type, s.queue, s.client_id, s.payload, s.interval_seconds, s.enabled = type, queue, client_id, payload or {}, interval_seconds, enabled
    db.flush()
    return s


def remove_schedule(db: Session, key: str) -> None:
    s = db.get(JobSchedule, key)
    if s:
        db.delete(s)
        db.flush()


def materialize_schedules(db: Session, *, now: datetime | None = None) -> int:
    now = now or utcnow()
    created_n = 0
    for s in db.scalars(select(JobSchedule).where(JobSchedule.enabled.is_(True), JobSchedule.next_run_at <= now)).all():
        job, created = enqueue(db, s.type, payload=s.payload, queue=s.queue, client_id=s.client_id,
                               idempotency_key=f"schedule:{s.key}:{s.next_run_at.isoformat()}")
        created_n += int(created)
        nxt = s.next_run_at + timedelta(seconds=s.interval_seconds)
        if nxt <= now:  # skip missed slots after downtime instead of replaying a backlog
            nxt = now + timedelta(seconds=s.interval_seconds)
        s.next_run_at, s.last_job_id = nxt, job.id
    db.commit()
    return created_n


def queue_stats(db: Session) -> dict[str, Any]:
    by_status = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    oldest = db.scalar(select(func.min(Job.run_at)).where(Job.status == "queued", Job.run_at <= utcnow()))
    return {"by_status": by_status, "oldest_ready_job_at": oldest.isoformat() if oldest else None}


def get_flag(db: Session, key: str, default: Any = None) -> Any:
    f = db.get(SystemFlag, key)
    return default if f is None else f.value


def set_flag(db: Session, key: str, value: Any, updated_by: str | None = None) -> None:
    f = db.get(SystemFlag, key)
    if not f:
        f = SystemFlag(key=key, value=value)
        db.add(f)
    f.value, f.updated_by, f.updated_at = value, updated_by, utcnow()
    db.flush()

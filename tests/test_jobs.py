from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core import jobs
from app.core.time import utcnow
from app.core.worker import NonRetryableError, Worker
from app.models import Job, JobSchedule, WorkerHeartbeat


def test_enqueue_idempotent(session):
    j1, c1 = jobs.enqueue(session, "x", idempotency_key="k1")
    j2, c2 = jobs.enqueue(session, "x", idempotency_key="k1")
    session.commit()
    assert c1 and not c2 and j1.id == j2.id


def test_retry_backoff_then_dead(session):
    calls = []

    def boom(db, job, payload):
        calls.append(job.attempts)
        raise RuntimeError("provider down")
    job, _ = jobs.enqueue(session, "boom", max_attempts=3)
    session.commit()
    w = Worker({"boom": boom}, worker_id="w1")
    now = utcnow()
    for i in range(5):
        w.tick(now=now + timedelta(hours=3 * i))
    session.expire_all()
    j = session.get(Job, job.id)
    assert j.status == "dead" and j.attempts == 3 and calls == [1, 2, 3] and "provider down" in j.last_error


def test_non_retryable_fails_immediately(session):
    def bad(db, job, payload):
        raise NonRetryableError("invalid payload")
    job, _ = jobs.enqueue(session, "bad")
    session.commit()
    Worker({"bad": bad}).tick()
    session.expire_all()
    assert session.get(Job, job.id).status == "failed"


def test_crash_recovery_requeues_expired_lease(session):
    job, _ = jobs.enqueue(session, "work")
    session.commit()
    claimed = jobs.claim_next(session, "dead-worker", lease_seconds=60)
    assert claimed.id == job.id and claimed.status == "running"
    # the worker "dies"; a new worker starts later and recovers the job
    ran = []
    w = Worker({"work": lambda db, j, p: ran.append(j.id) or {"ok": True}}, worker_id="w2")
    later = utcnow() + timedelta(minutes=5)
    w.tick(now=later)                       # recovers -> queued with backoff
    w.tick(now=later + timedelta(hours=2))  # runs it
    session.expire_all()
    assert ran == [job.id] and session.get(Job, job.id).status == "succeeded"


def test_two_workers_never_claim_same_job(session):
    jobs.enqueue(session, "only-one")
    session.commit()
    a = jobs.claim_next(session, "A")
    b = jobs.claim_next(session, "B")
    assert a is not None and b is None


def test_schedule_materializes_once_per_slot_and_survives_restart(session):
    start = utcnow() - timedelta(seconds=1)
    jobs.upsert_schedule(session, "tick", "noop", 60, first_run_at=start)
    session.commit()
    assert jobs.materialize_schedules(session) == 1
    assert jobs.materialize_schedules(session) == 0  # same slot, no double fire
    s = session.get(JobSchedule, "tick")
    assert s.next_run_at > utcnow()
    # long downtime: missed slots are skipped, not replayed
    assert jobs.materialize_schedules(session, now=utcnow() + timedelta(hours=5)) == 1
    assert len(session.scalars(select(Job).where(Job.type == "noop")).all()) == 2


def test_worker_heartbeat_and_health(session):
    from app.core.worker import worker_health
    Worker({}, worker_id="hb").tick()
    session.expire_all()
    assert session.get(WorkerHeartbeat, "hb").status == "idle"
    assert worker_health(session)[0]["status"] == "idle"

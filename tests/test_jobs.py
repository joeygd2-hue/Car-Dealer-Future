"""Durable queue: idempotency, retries/backoff, dead-lettering, deferral, lease recovery, scheduler."""

from datetime import timedelta

from sqlalchemy import select, update

from app import db as dbmod
from app.models import Job, RecurringTask, WorkerHeartbeat
from app.services import jobs
from app.worker import Worker, worker_health

CALLS = {"n": 0}


@jobs.handler("test.flaky")
def _flaky(db, payload, job):
    CALLS["n"] += 1
    if CALLS["n"] < payload.get("succeed_on", 99):
        raise RuntimeError("transient")
    return {"ok": True}


@jobs.handler("test.defer")
def _defer(db, payload, job):
    raise jobs.RetryLater(600, "outside window")


@jobs.handler("test.permanent")
def _perm(db, payload, job):
    raise jobs.PermanentJobError("bad input")


def _due_now(db):
    db.execute(update(Job).values(run_at=dbmod.utcnow() - timedelta(seconds=1)))
    db.commit()


def test_enqueue_is_idempotent(db):
    a = jobs.enqueue(db, "test.flaky", {}, idempotency_key="k1")
    b = jobs.enqueue(db, "test.flaky", {}, idempotency_key="k1")
    db.commit()
    assert a.id == b.id
    assert db.scalar(select(Job).where(Job.idempotency_key == "k1")) is not None
    assert len(list(db.scalars(select(Job)))) == 1


def test_retry_with_backoff_then_success(db):
    CALLS["n"] = 0
    job = jobs.enqueue(db, "test.flaky", {"succeed_on": 3}, max_attempts=5)
    db.commit()
    w = Worker(worker_id="w1", poll_seconds=0)
    for _ in range(3):
        _due_now(db)
        w.run_once()
    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == "SUCCEEDED" and job.attempts == 3


def test_backoff_grows():
    assert jobs.backoff_seconds(1, jitter=False) < jobs.backoff_seconds(3, jitter=False) <= 3600


def test_dead_after_max_attempts(db):
    CALLS["n"] = 0
    job = jobs.enqueue(db, "test.flaky", {"succeed_on": 99}, max_attempts=2)
    db.commit()
    w = Worker(worker_id="w1", poll_seconds=0)
    for _ in range(3):
        _due_now(db)
        w.run_once()
    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == "DEAD" and "transient" in job.last_error


def test_permanent_error_dead_immediately(db):
    job = jobs.enqueue(db, "test.permanent", {})
    db.commit()
    Worker(worker_id="w1", poll_seconds=0).run_once()
    db.expire_all()
    assert db.get(Job, job.id).status == "DEAD"


def test_retry_later_does_not_consume_attempts(db):
    job = jobs.enqueue(db, "test.defer", {}, max_attempts=1)
    db.commit()
    Worker(worker_id="w1", poll_seconds=0).run_once()
    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == "QUEUED" and job.attempts == 0 and job.run_at > dbmod.utcnow() + timedelta(seconds=500)


def test_expired_lease_is_recovered_after_crash(db):
    """Simulates a worker that died mid-job: another worker reclaims the job after the lease expires."""
    CALLS["n"] = 0
    job = jobs.enqueue(db, "test.flaky", {"succeed_on": 1})
    db.commit()
    claimed = jobs.claim_next(db, "crashed-worker", lease_seconds=60)
    assert claimed == job.id
    # Lease still valid → nobody else can claim it.
    assert jobs.claim_next(db, "other", lease_seconds=60) is None
    db.execute(update(Job).where(Job.id == job.id).values(locked_until=dbmod.utcnow() - timedelta(seconds=1)))
    db.commit()
    Worker(worker_id="recovery-worker", poll_seconds=0).run_once()
    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == "SUCCEEDED" and job.attempts == 2


def test_scheduler_enqueues_once_per_slot(db):
    jobs.ensure_recurring(db, "every-minute", "test.flaky", 60, {"succeed_on": 1})
    db.commit()
    s1, s2 = dbmod.SessionLocal(), dbmod.SessionLocal()
    first = jobs.scheduler_tick(s1)
    second = jobs.scheduler_tick(s2)  # a second scheduler (or a restart) in the same slot
    s1.close()
    s2.close()
    assert first == ["every-minute"] and second == []
    assert len(list(db.scalars(select(Job).where(Job.type == "test.flaky")))) == 1
    task = db.scalar(select(RecurringTask))
    assert task.next_run_at > dbmod.utcnow()


def test_worker_heartbeat_health(db):
    w = Worker(worker_id="hb", poll_seconds=0)
    w.run_once()
    assert worker_health(db)["healthy"] is True
    db.execute(update(WorkerHeartbeat).values(last_seen_at=dbmod.utcnow() - timedelta(minutes=10)))
    db.commit()
    assert worker_health(db)["healthy"] is False

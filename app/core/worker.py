"""Persistent background worker.

Each ``tick`` is bounded: recover expired leases, materialise due schedules,
then run at most ``max_jobs`` jobs, each with a time budget. All progress is in
the database, so the process can be killed and restarted at any time.

Run with: ``python -m app.worker``
"""
from __future__ import annotations

import os
import signal
import socket
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

from app.core import jobs
from app.core.time import utcnow
from app.db import SessionLocal
from app.models import Job, WorkerHeartbeat

Handler = Callable[[Any, Job, dict], dict | None]


class NonRetryableError(Exception):
    """Raise from a handler when retrying cannot help (validation, policy)."""


class Worker:
    def __init__(self, handlers: dict[str, Handler], *, queues: list[str] | None = None, lease_seconds: int = 300,
                 job_timeout_seconds: int = 120, worker_id: str | None = None):
        self.handlers = handlers
        self.queues = queues
        self.lease_seconds = lease_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.processed = 0
        self.started_at = utcnow()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=1)

    def heartbeat(self, db, status: str, **info) -> None:
        hb = db.get(WorkerHeartbeat, self.worker_id)
        if not hb:
            hb = WorkerHeartbeat(worker_id=self.worker_id, started_at=self.started_at, status=status)
            db.add(hb)
        hb.status, hb.last_beat_at, hb.info = status, utcnow(), {"processed": self.processed, **info}
        db.commit()

    def tick(self, max_jobs: int = 25, now=None) -> int:
        db = SessionLocal()
        try:
            jobs.recover_expired_leases(db, now=now)
            jobs.materialize_schedules(db, now=now)
            ran = 0
            while ran < max_jobs and not self._stop.is_set():
                job = jobs.claim_next(db, self.worker_id, queues=self.queues, lease_seconds=self.lease_seconds, now=now)
                if not job:
                    break
                self.run_job(db, job)
                ran += 1
            self.heartbeat(db, "idle")
            return ran
        finally:
            db.close()

    def run_job(self, db, job: Job) -> None:
        handler = self.handlers.get(job.type)
        if not handler:
            jobs.fail_job(db, job, f"No handler registered for {job.type}", retryable=False)
            return
        self.heartbeat(db, "running", job_id=job.id, type=job.type)
        payload = dict(job.payload or {})

        def _run():
            hdb = SessionLocal()
            try:
                result = handler(hdb, job, payload)
                hdb.commit()
                return result
            except Exception:
                hdb.rollback()
                raise
            finally:
                hdb.close()

        future = self._pool.submit(_run)
        try:
            result = future.result(timeout=self.job_timeout_seconds)
            jobs.complete_job(db, job, result if isinstance(result, dict) else {})
        except FutureTimeout:
            # Lease will expire and the job is retried; the stuck thread is abandoned.
            jobs.fail_job(db, job, f"Job exceeded {self.job_timeout_seconds}s budget")
            self._pool = ThreadPoolExecutor(max_workers=1)
        except NonRetryableError as exc:
            jobs.fail_job(db, job, exc, retryable=False)
        except Exception as exc:  # noqa: BLE001
            jobs.fail_job(db, job, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
        finally:
            self.processed += 1

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        while not self._stop.is_set():
            try:
                if not self.tick():
                    self._stop.wait(poll_seconds)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._stop.wait(poll_seconds * 5)
        db = SessionLocal()
        try:
            self.heartbeat(db, "stopped")
        finally:
            db.close()

    def stop(self) -> None:
        self._stop.set()


def worker_health(db, stale_after_seconds: int = 120) -> list[dict[str, Any]]:
    from sqlalchemy import select
    out = []
    for hb in db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_beat_at.desc())).all():
        age = (utcnow() - hb.last_beat_at).total_seconds()
        out.append({"worker_id": hb.worker_id, "status": hb.status if age < stale_after_seconds or hb.status == "stopped" else "unresponsive",
                    "last_beat_at": hb.last_beat_at.isoformat(), "seconds_since_beat": round(age), "info": hb.info})
    return out


def _sleep(seconds: float) -> None:
    time.sleep(seconds)

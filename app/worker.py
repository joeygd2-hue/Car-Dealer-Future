"""Persistent background worker: scheduler + job execution + heartbeat.

Run:  python -m app.worker            (loop until SIGTERM/SIGINT)
      python -m app.worker --once     (process due work once, e.g. from cron or tests)

Several workers may run concurrently; claims are atomic. State lives in the database, so
restarting a worker resumes where it left off (expired leases are reclaimed).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, utcnow
from app.models import WorkerHeartbeat
from app.services import jobs

log = logging.getLogger("app.worker")


def register_all_handlers() -> None:
    """Import modules that register job handlers and recurring tasks."""
    import app.services.appointments  # noqa: F401
    import app.services.inbox.processing  # noqa: F401
    import app.services.maintenance  # noqa: F401
    import app.services.notifications  # noqa: F401
    import app.services.outreach.agent  # noqa: F401


def install_recurring_tasks() -> None:
    from app.services import maintenance
    from app.services.outreach import agent

    with SessionLocal() as db:
        maintenance.install_schedules(db)
        agent.install_schedules(db)
        db.commit()


class Worker:
    def __init__(self, worker_id: str | None = None, lease_seconds: int | None = None, poll_seconds: float | None = None):
        settings = get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.lease_seconds = lease_seconds or settings.worker_lease_seconds
        self.poll_seconds = poll_seconds if poll_seconds is not None else settings.worker_poll_seconds
        self._stop = threading.Event()
        self.succeeded = 0
        self.failed = 0

    def stop(self, *_args) -> None:
        log.info("worker %s stopping", self.worker_id)
        self._stop.set()

    def heartbeat(self, status: str = "RUNNING") -> None:
        with SessionLocal() as db:
            hb = db.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == self.worker_id))
            if hb is None:
                hb = WorkerHeartbeat(worker_id=self.worker_id, hostname=socket.gethostname())
                db.add(hb)
            hb.last_seen_at = utcnow()
            hb.status = status
            hb.jobs_succeeded = self.succeeded
            hb.jobs_failed = self.failed
            db.commit()

    def run_once(self, max_jobs: int = 50) -> int:
        """One bounded iteration: heartbeat, schedule, then run up to ``max_jobs`` due jobs."""
        self.heartbeat()
        with SessionLocal() as db:
            jobs.scheduler_tick(db)
        processed = 0
        while processed < max_jobs and not self._stop.is_set():
            with SessionLocal() as db:
                job_id = jobs.claim_next(db, self.worker_id, self.lease_seconds)
            if job_id is None:
                break
            status = jobs.run_job(job_id, self.worker_id)
            processed += 1
            if status == jobs.SUCCEEDED:
                self.succeeded += 1
            elif status == jobs.DEAD:
                self.failed += 1
        return processed

    def run_forever(self) -> None:
        log.info("worker %s started", self.worker_id)
        while not self._stop.is_set():
            try:
                processed = self.run_once()
            except Exception:  # noqa: BLE001 - never let the loop die; DB may be briefly unavailable
                log.exception("worker iteration failed")
                processed = 0
            if processed == 0:
                self._stop.wait(self.poll_seconds)
        self.heartbeat("STOPPED")


def worker_health(db, stale_seconds: int | None = None) -> dict:
    stale = stale_seconds or get_settings().worker_heartbeat_stale_seconds
    now = utcnow()
    beats = list(db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()).limit(10)))
    alive = [b for b in beats if b.status == "RUNNING" and (now - b.last_seen_at).total_seconds() <= stale]
    return {
        "healthy": bool(alive),
        "alive_workers": [b.worker_id for b in alive],
        "last_seen_at": beats[0].last_seen_at.isoformat() if beats else None,
        "queue": jobs.queue_counts(db),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.config import validate_settings

    for warning in validate_settings(get_settings()):  # raises ConfigError on unsafe configuration
        log.warning("config: %s", warning)
    register_all_handlers()
    install_recurring_tasks()
    worker = Worker()
    if args.once:
        n = worker.run_once()
        worker.heartbeat("STOPPED")
        print(f"processed {n} jobs")
        return
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()

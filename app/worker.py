"""Persistent background worker: `python -m app.worker`.

Loop: heartbeat -> recover expired leases -> enqueue due scheduled tasks ->
claim and run jobs until none are due -> sleep. Safe to run several copies
(claims are atomic; scheduled slots are idempotent). SIGTERM/SIGINT finish
the current job and exit cleanly; anything interrupted mid-job is recovered
by lease expiry on the next start.
"""
from __future__ import annotations

import logging
import signal
import time

from app.config import get_settings
from app.db import SessionLocal
from app.main import _register_job_handlers, configure_logging
from app.services import jobs

log = logging.getLogger("worker")
_stop = False


def _handle_signal(signum, _frame) -> None:
    global _stop
    log.info("Received signal %s; finishing current job then exiting", signum)
    _stop = True


def run_once(worker_id: str, max_jobs: int = 50) -> int:
    db = SessionLocal()
    try:
        jobs.heartbeat(db, worker_id)
        recovered = jobs.recover_expired_leases(db)
        if recovered:
            log.warning("Recovered %d job(s) with expired leases", recovered)
        jobs.schedule_due_tasks(db)
        done = 0
        while done < max_jobs and not _stop:
            job = jobs.claim_next(db, worker_id)
            if job is None:
                break
            log.info("Running job %s (%s) attempt %s", job.id, job.kind, job.attempts)
            jobs.run_job(db, job)
            done += 1
        if done:
            jobs.heartbeat(db, worker_id, processed=done)
        return done
    finally:
        db.close()


def main() -> None:
    configure_logging()
    _register_job_handlers()
    from app.services.maintenance import bootstrap

    db = SessionLocal()
    try:
        bootstrap(db)
    finally:
        db.close()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    worker_id = jobs.default_worker_id()
    poll = get_settings().worker_poll_seconds
    log.info("Worker %s started; handlers: %s", worker_id, ", ".join(jobs.registered_kinds()))
    while not _stop:
        try:
            run_once(worker_id)
        except Exception:  # noqa: BLE001 — never let the loop die; log and back off
            log.exception("Worker loop error")
            time.sleep(min(60, poll * 4))
        time.sleep(poll)
    log.info("Worker %s stopped", worker_id)


if __name__ == "__main__":
    main()

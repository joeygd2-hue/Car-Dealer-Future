"""Entry point for the persistent background worker: ``python -m app.worker``."""
from __future__ import annotations

import logging

from app.config import settings
from app.core.worker import Worker
from app.db import SessionLocal


def build_handlers() -> dict:
    from app.outreach.jobs import HANDLERS as OUTREACH
    from app.sync.jobs import HANDLERS as SYNC
    return {**SYNC, **OUTREACH}


def ensure_schedules() -> None:
    from app.outreach.jobs import ensure_outreach_schedules
    from app.sync.jobs import ensure_platform_schedules
    db = SessionLocal()
    try:
        ensure_platform_schedules(db)
        ensure_outreach_schedules(db)
        db.commit()
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ensure_schedules()
    Worker(build_handlers()).run_forever(poll_seconds=settings.worker_poll_seconds)


if __name__ == "__main__":
    main()

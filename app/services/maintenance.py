"""Recurring housekeeping jobs (schedules installed by the worker)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import InboundEvent, Job
from app.services import jobs

RAW_EVENT_RETENTION_DAYS = 30
FINISHED_JOB_RETENTION_DAYS = 30


def install_schedules(db: Session) -> None:
    jobs.ensure_recurring(db, "notifications.daily_digests", "notification.daily_digests", 3600)
    jobs.ensure_recurring(db, "maintenance.retention", "maintenance.retention", 6 * 3600)
    jobs.ensure_recurring(db, "appointments.reminders", "appointments.dispatch_reminders", 300)


@jobs.handler("maintenance.retention")
def retention_job(db: Session, payload: dict, job) -> dict:
    """Purge raw provider payloads (data minimization) and old finished jobs."""
    cutoff = utcnow() - timedelta(days=RAW_EVENT_RETENTION_DAYS)
    ev = db.execute(delete(InboundEvent).where(InboundEvent.received_at < cutoff)).rowcount
    jcut = utcnow() - timedelta(days=FINISHED_JOB_RETENTION_DAYS)
    jb = db.execute(delete(Job).where(Job.status.in_(["SUCCEEDED", "CANCELLED"]), Job.finished_at < jcut)).rowcount
    from app.services.outreach.discovery import purge_expired_source_content

    purged = purge_expired_source_content(db)
    return {"events_deleted": ev, "jobs_deleted": jb, "prospect_fields_purged": purged}

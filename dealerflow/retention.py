"""Data retention: redact old message bodies and prune operational logs.

Kept forever on purpose: the suppression list (opt-outs must keep working), prospects and their
evidence (business records), audit log entries (accountability). Message metadata (who, when,
status) is kept; only the bodies are redacted.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from .audit import audit
from .db import utcnow
from .models import AgentRun, InboundMessage, IntegrationLog, Job, OutreachMessage

REDACTED = "[redacted under retention policy]"


def apply(db: Session, *, message_days: int = 730, log_days: int = 365, job_days: int = 30) -> dict:
    now = utcnow()
    res = {
        "inbound_redacted": db.execute(update(InboundMessage).where(
            InboundMessage.received_at < now - timedelta(days=message_days), InboundMessage.body != REDACTED)
            .values(body=REDACTED)).rowcount,
        "outreach_redacted": db.execute(update(OutreachMessage).where(
            OutreachMessage.created_at < now - timedelta(days=message_days), OutreachMessage.body != REDACTED,
            OutreachMessage.status.notin_(("draft", "approved", "queued", "sending"))).values(body=REDACTED)).rowcount,
        "integration_log_deleted": db.execute(delete(IntegrationLog).where(
            IntegrationLog.created_at < now - timedelta(days=log_days))).rowcount,
        "agent_runs_deleted": db.execute(delete(AgentRun).where(AgentRun.started_at < now - timedelta(days=log_days))).rowcount,
        "jobs_deleted": db.execute(delete(Job).where(Job.status.in_(("done", "failed")),
                                                     Job.finished_at < now - timedelta(days=job_days))).rowcount,
    }
    audit(db, "retention.apply", data={"message_days": message_days, "log_days": log_days, **res})
    return res

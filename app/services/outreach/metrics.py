"""Operator dashboard metrics. Measured values come from stored records; estimates are labeled."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    CostEntry,
    Enrollment,
    Job,
    OutreachMessage,
    OutreachReply,
    Prospect,
    SalesMeeting,
)
from app.services import jobs


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def dashboard(db: Session) -> dict:
    now = utcnow()
    sent_statuses = ("SENT", "SIMULATED")
    attempts_live = _count(db, select(func.count(OutreachMessage.id)).where(OutreachMessage.status == "SENT",
                                                                           OutreachMessage.kind == "SEQUENCE"))
    attempts_sim = _count(db, select(func.count(OutreachMessage.id)).where(OutreachMessage.status == "SIMULATED",
                                                                          OutreachMessage.kind == "SEQUENCE"))
    replies = _count(db, select(func.count(OutreachReply.id)).where(OutreachReply.classification.not_in(["BOUNCE", "OUT_OF_OFFICE"])))
    positive = _count(db, select(func.count(OutreachReply.id)).where(OutreachReply.classification.in_(["POSITIVE", "MEETING_REQUEST"])))
    opt_outs = _count(db, select(func.count(OutreachReply.id)).where(OutreachReply.classification == "OPT_OUT"))
    meetings = _count(db, select(func.count(SalesMeeting.id)).where(SalesMeeting.status == "CONFIRMED"))
    meetings_sim = _count(db, select(func.count(SalesMeeting.id)).where(SalesMeeting.status == "CONFIRMED",
                                                                       SalesMeeting.simulated.is_(True)))
    attempts = attempts_live + attempts_sim
    cost_actual = float(db.scalar(select(func.coalesce(func.sum(CostEntry.amount_cents), 0.0)).where(
        CostEntry.scope == "OUTREACH", CostEntry.is_estimate.is_(False))) or 0.0)
    cost_est = float(db.scalar(select(func.coalesce(func.sum(CostEntry.amount_cents), 0.0)).where(
        CostEntry.scope == "OUTREACH", CostEntry.is_estimate.is_(True))) or 0.0)
    cost_est_7d = float(db.scalar(select(func.coalesce(func.sum(CostEntry.amount_cents), 0.0)).where(
        CostEntry.scope == "OUTREACH", CostEntry.created_at >= now - timedelta(days=7))) or 0.0)
    status_counts = dict(db.execute(select(Prospect.status, func.count()).group_by(Prospect.status)).all())
    return {
        "measured": {
            "prospects_total": sum(status_counts.values()),
            "prospects_by_status": status_counts,
            "prospects_researched": sum(v for k, v in status_counts.items() if k != "NEW"),
            "outreach_attempts_live": attempts_live,
            "outreach_attempts_simulated": attempts_sim,
            "pending_approval": _count(db, select(func.count(OutreachMessage.id)).where(OutreachMessage.status == "PENDING_APPROVAL")),
            "blocked_messages": _count(db, select(func.count(OutreachMessage.id)).where(OutreachMessage.status == "BLOCKED")),
            "replies": replies,
            "positive_replies": positive,
            "opt_outs": opt_outs,
            "replies_needing_human": _count(db, select(func.count(OutreachReply.id)).where(OutreachReply.requires_human.is_(True))),
            "follow_ups_due": _count(db, select(func.count(Enrollment.id)).where(Enrollment.status == "ACTIVE",
                                                                                Enrollment.next_action_at <= now)),
            "active_enrollments": _count(db, select(func.count(Enrollment.id)).where(Enrollment.status == "ACTIVE")),
            "meetings_booked": meetings,
            "meetings_booked_simulated": meetings_sim,
            "dead_jobs": _count(db, select(func.count(Job.id)).where(Job.type.startswith("outreach."), Job.status == "DEAD")),
            "queue": jobs.queue_counts(db, "outreach."),
            "sent_last_24h": _count(db, select(func.count(OutreachMessage.id)).where(
                OutreachMessage.status.in_(sent_statuses), OutreachMessage.sent_at >= now - timedelta(days=1))),
        },
        "rates": {  # computed from measured counts; "n/a" when the denominator is zero
            "reply_rate": _rate(replies, attempts),
            "positive_reply_rate": _rate(positive, attempts),
            "meeting_rate": _rate(meetings, attempts),
            "opt_out_rate": _rate(opt_outs, attempts),
        },
        "costs": {
            "recorded_actual_cents": round(cost_actual, 2),
            "estimated_cents": round(cost_est, 2),
            "estimated_last_7d_cents": round(cost_est_7d, 2),
            "note": "Estimates use published list prices; provider invoices are the source of truth.",
        },
    }


def _rate(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"

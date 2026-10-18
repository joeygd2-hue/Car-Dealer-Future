"""Operator dashboard metrics. Measured values come from recorded events; estimates are labelled."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.jobs import get_flag, queue_stats
from app.core.time import utcnow
from app.core.worker import worker_health
from app.models import (
    AgentRun, Campaign, CostEntry, Enrollment, Escalation, Job, Meeting, OutreachMessage, PlatformIntegration, Prospect, Suppression,
)
from app.outreach import agent, policy

POSITIVE = ("positive_interest", "meeting_request", "meeting_slot_selection")


def _n(db, q) -> int:
    return db.scalar(q) or 0


def dashboard(db: Session, days: int = 30) -> dict[str, Any]:
    now = utcnow()
    since = now - timedelta(days=days)
    out_q = select(func.count()).select_from(OutreachMessage).where(OutreachMessage.direction == "outbound")
    sent = _n(db, out_q.where(OutreachMessage.status == "sent", OutreachMessage.sent_at >= since))
    sent_first = _n(db, out_q.where(OutreachMessage.status == "sent", OutreachMessage.step == 1, OutreachMessage.sent_at >= since))
    in_q = select(func.count()).select_from(OutreachMessage).where(OutreachMessage.direction == "inbound", OutreachMessage.received_at >= since)
    replies = _n(db, in_q.where(OutreachMessage.classification.not_in(("bounce", "out_of_office"))))
    positive = _n(db, in_q.where(OutreachMessage.classification.in_(POSITIVE)))
    opt_outs = _n(db, select(func.count()).select_from(Suppression).where(Suppression.reason == "opt_out", Suppression.created_at >= since))
    bounces = _n(db, in_q.where(OutreachMessage.classification == "bounce"))
    meetings = _n(db, select(func.count()).select_from(Meeting).where(Meeting.status == "booked", Meeting.created_at >= since))
    researched = _n(db, select(func.count()).select_from(Prospect).where(Prospect.last_enriched_at >= since))
    costs = db.execute(select(CostEntry.category, CostEntry.measured, func.sum(CostEntry.amount_usd), func.count())
                       .where(CostEntry.created_at >= since).group_by(CostEntry.category, CostEntry.measured)).all()
    last_run = db.scalar(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(1))
    rate = lambda a, b: round(a / b, 4) if b else None  # noqa: E731
    simulated_sent = _n(db, out_q.join(Prospect, Prospect.id == OutreachMessage.prospect_id)
                        .where(OutreachMessage.status == "sent", OutreachMessage.sent_at >= since, Prospect.is_simulated.is_(True)))
    return {
        "generated_at": now.isoformat(), "period_days": days,
        "agent": {"state": agent.agent_state(db), "global_pause": bool(get_flag(db, policy.GLOBAL_PAUSE, False)),
                  "emergency_stop": bool(get_flag(db, policy.EMERGENCY_STOP, False)),
                  "last_run": {"id": last_run.id, "state": last_run.state, "phase": last_run.phase, "stats": last_run.stats,
                               "started_at": last_run.started_at.isoformat(),
                               "finished_at": last_run.finished_at.isoformat() if last_run.finished_at else None} if last_run else None,
                  "workers": worker_health(db), "queue": queue_stats(db)},
        "measured": {
            "prospects_total": _n(db, select(func.count()).select_from(Prospect)),
            "prospects_by_status": dict(db.execute(select(Prospect.status, func.count()).group_by(Prospect.status)).all()),
            "prospects_researched": researched,
            "outreach_attempts_sent": sent, "first_touches_sent": sent_first,
            "sent_to_simulated_prospects": simulated_sent,
            "replies": replies, "positive_replies": positive, "opt_outs": opt_outs, "bounces": bounces,
            "follow_ups_due": _n(db, select(func.count()).select_from(Enrollment).where(Enrollment.state == "waiting",
                                                                                          Enrollment.next_action_at <= now)),
            "awaiting_approval": _n(db, out_q.where(OutreachMessage.status == "awaiting_approval")),
            "meetings_booked": meetings,
            "open_escalations": _n(db, select(func.count()).select_from(Escalation).where(Escalation.status == "open")),
            "errors": {"failed_messages": _n(db, out_q.where(OutreachMessage.status == "failed", OutreachMessage.updated_at >= since)),
                       "blocked_messages": _n(db, out_q.where(OutreachMessage.status == "blocked", OutreachMessage.updated_at >= since)),
                       "dead_jobs": _n(db, select(func.count()).select_from(Job).where(Job.status.in_(("dead", "failed")),
                                                                                       Job.type.startswith("outreach."), Job.updated_at >= since))},
            "conversion": {"reply_rate": rate(replies, sent_first), "positive_reply_rate": rate(positive, sent_first),
                           "meeting_rate": rate(meetings, sent_first), "opt_out_rate": rate(opt_outs, sent_first)},
        },
        "costs": {
            "measured_usd": round(sum(t for c, m, t, n in costs if m), 4),
            "estimated_usd": round(sum(t for c, m, t, n in costs if not m), 4),
            "by_category": [{"category": c, "measured": bool(m), "usd": round(t or 0, 4), "entries": n} for c, m, t, n in costs],
            "note": "AI costs are measured from API token usage; Places/email costs are estimates from published price lists.",
        },
        "integrations": [{"id": i.id, "kind": i.kind, "provider": i.provider, "name": i.name, "live": i.provider != "sandbox",
                          "live_authorized": i.live_authorized, "health": i.health_status, "detail": i.health_detail,
                          "checked_at": i.health_checked_at.isoformat() if i.health_checked_at else None}
                         for i in db.scalars(select(PlatformIntegration)).all()],
        "campaigns": [{"id": c.id, "name": c.name, "status": c.status, "live_authorized": c.live_authorized,
                       "enrollments": _n(db, select(func.count()).select_from(Enrollment).where(Enrollment.campaign_id == c.id))}
                      for c in db.scalars(select(Campaign)).all()],
    }

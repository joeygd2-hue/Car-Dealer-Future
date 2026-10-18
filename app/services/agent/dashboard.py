"""Operator dashboard metrics. Measured counts come from recorded rows; costs are labelled by basis."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.base import utcnow
from app.models.core import IntegrationConnection, Job, WorkerHeartbeat
from app.models.outreach import AgentActivity, Enrollment, InboundMessage, Meeting, OutreachMessage, Prospect, Suppression
from app.models.costs import UsageRecord
from app.services.agent.compliance import agent_spend_today_micros, in_send_window
from app.services.agent.state import get_config, live_mode_blockers


def _count(db: Session, model, *conds) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*conds)) or 0)


def _pct(a: int, b: int) -> float | None:
    return round(a * 100 / b, 1) if b else None


def agent_dashboard(db: Session, days: int = 30) -> dict:
    cfg = get_config(db)
    since = utcnow() - timedelta(days=days)
    sent = _count(db, OutreachMessage, OutreachMessage.status == "sent", OutreachMessage.sent_at >= since, OutreachMessage.kind != "reply")
    sandboxed = _count(db, OutreachMessage, OutreachMessage.status == "sent", OutreachMessage.provider == "sandbox", OutreachMessage.sent_at >= since)
    replies = _count(db, InboundMessage, InboundMessage.received_at >= since, InboundMessage.prospect_id.is_not(None),
                     InboundMessage.classification.notin_(("bounce", "out_of_office")))
    positive = _count(db, InboundMessage, InboundMessage.received_at >= since, InboundMessage.classification.in_(("positive", "meeting_request")))
    optouts = _count(db, Suppression, Suppression.reason == "opt_out", Suppression.created_at >= since)
    bounces = _count(db, Suppression, Suppression.reason == "bounce", Suppression.created_at >= since)
    meetings = _count(db, Meeting, Meeting.created_at >= since, Meeting.status != "cancelled")
    errors = _count(db, AgentActivity, AgentActivity.outcome == "error", AgentActivity.created_at >= since)
    hb = db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc())).all()
    worker_alive = bool(hb and hb[0].last_seen_at > utcnow() - timedelta(minutes=2))
    stages = dict(db.execute(select(Prospect.stage, func.count()).group_by(Prospect.stage)).all())
    today = datetime(utcnow().year, utcnow().month, utcnow().day)
    month_start = datetime(today.year, today.month, 1)
    cost_rows = db.execute(
        select(UsageRecord.cost_basis, func.coalesce(func.sum(UsageRecord.cost_micros), 0), func.count())
        .where(UsageRecord.feature.like("agent%"), UsageRecord.occurred_at >= month_start)
        .group_by(UsageRecord.cost_basis)
    ).all()
    s = get_settings()
    integrations = [
        {"name": "Email sending", "provider": s.email_provider if cfg.live_mode else "sandbox", "live": cfg.live_mode and s.email_provider != "sandbox"},
        {"name": "Inbox monitoring", "provider": s.inbox_provider if cfg.live_mode else "sandbox", "live": cfg.live_mode and s.inbox_provider != "sandbox"},
        {"name": "Calendar / video", "provider": s.calendar_provider if cfg.live_mode else "sandbox", "live": cfg.live_mode and s.calendar_provider != "sandbox"},
        {"name": "Prospect discovery (Google Places)", "provider": "google_places" if s.google_places_api_key else "not configured", "live": bool(s.google_places_api_key)},
        {"name": "AI (classification/drafting)", "provider": s.ai_model if s.ai_enabled and s.anthropic_api_key else "disabled", "live": bool(s.ai_enabled and s.anthropic_api_key)},
    ]
    for conn in db.scalars(select(IntegrationConnection).where(IntegrationConnection.client_id.is_(None))):
        integrations.append({"name": f"{conn.kind} ({conn.provider})", "provider": conn.provider, "live": conn.status == "connected", "status": conn.status, "last_error": conn.last_error})
    return {
        "window_days": days,
        "agent": {
            "state": cfg.state, "state_reason": cfg.state_reason, "live_mode": cfg.live_mode, "live_blockers": live_mode_blockers(),
            "in_send_window_now": in_send_window(cfg), "last_tick_at": cfg.last_tick_at.isoformat() if cfg.last_tick_at else None,
            "last_tick_summary": cfg.last_tick_summary,
        },
        "worker": {"alive": worker_alive, "last_seen": hb[0].last_seen_at.isoformat() if hb else None, "workers": len(hb)},
        "queue": {
            "jobs": dict(db.execute(select(Job.status, func.count()).where(Job.kind.like("agent.%")).group_by(Job.status)).all()),
            "pending_approval": _count(db, OutreachMessage, OutreachMessage.status == "pending_approval"),
            "approved_unsent": _count(db, OutreachMessage, OutreachMessage.status == "approved"),
            "needs_human_replies": _count(db, InboundMessage, InboundMessage.needs_human.is_(True), InboundMessage.handled_at.is_not(None)),
            "followups_due": _count(db, Enrollment, Enrollment.status == "active", Enrollment.next_action_at <= utcnow()),
        },
        "measured": {
            "prospects_by_stage": stages,
            "prospects_researched": _count(db, Prospect, Prospect.enriched_at >= since),
            "outreach_sent": sent,
            "outreach_sent_sandbox_only": sandboxed,
            "replies": replies,
            "positive_replies": positive,
            "opt_outs": optouts,
            "bounces": bounces,
            "meetings_booked": meetings,
            "errors": errors,
            "reply_rate_percent": _pct(replies, sent),
            "positive_rate_percent": _pct(positive, sent),
            "meeting_rate_percent": _pct(meetings, sent),
            "note": "Counts include sandbox (simulated) activity until live mode is authorized." if not cfg.live_mode else None,
        },
        "costs_this_month": {basis: {"micros": int(total), "records": n} for basis, total, n in cost_rows},
        "budget": {"daily_budget_micros": cfg.daily_budget_micros, "spent_today_micros": agent_spend_today_micros(db), "spent_today_basis": "includes estimates"},
        "integrations": integrations,
    }

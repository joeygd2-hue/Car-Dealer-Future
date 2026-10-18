"""Agent configuration, state machine, controls and activity log.

States:
  stopped            — not running (initial state). Ticks do nothing.
  running            — ticks execute bounded work.
  paused             — temporarily halted; queued work stays queued.
  emergency_stopped  — halted AND all unsent outbound messages cancelled.
                       Only an admin can clear it (to `stopped`).
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.base import utcnow
from app.models.core import Job, User
from app.models.outreach import AgentActivity, AgentConfig, Campaign, OutreachMessage
from app.services import audit

STATES = ("stopped", "running", "paused", "emergency_stopped")
TRANSITIONS = {
    "stopped": {"running", "emergency_stopped"},
    "running": {"paused", "stopped", "emergency_stopped"},
    "paused": {"running", "stopped", "emergency_stopped"},
    "emergency_stopped": {"stopped"},
}


class AgentError(ValueError):
    pass


def get_config(db: Session) -> AgentConfig:
    cfg = db.scalar(select(AgentConfig).order_by(AgentConfig.id).limit(1))
    if cfg is None:
        cfg = AgentConfig(timezone=get_settings().business_timezone)
        db.add(cfg)
        db.flush()
    return cfg


def log_activity(
    db: Session,
    action: str,
    outcome: str,
    *,
    detail: str | None = None,
    prospect_id: int | None = None,
    campaign_id: int | None = None,
    job_id: int | None = None,
    data: dict | None = None,
    cost_micros: int = 0,
    cost_basis: str | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
) -> AgentActivity:
    row = AgentActivity(
        action=action, outcome=outcome, detail=(detail or "")[:4000] or None, prospect_id=prospect_id, campaign_id=campaign_id,
        job_id=job_id, data=data or {}, cost_micros=cost_micros, cost_basis=cost_basis, from_state=from_state, to_state=to_state,
    )
    db.add(row)
    db.flush()
    return row


def set_state(db: Session, new_state: str, actor: User | None, reason: str = "") -> AgentConfig:
    cfg = get_config(db)
    if new_state not in STATES:
        raise AgentError("Unknown state")
    if new_state == cfg.state:
        return cfg
    if new_state not in TRANSITIONS[cfg.state]:
        raise AgentError(f"Cannot go from {cfg.state} to {new_state}")
    if cfg.state == "emergency_stopped" and (actor is None or not actor.is_admin):
        raise AgentError("Only an admin can clear an emergency stop")
    old = cfg.state
    cfg.state, cfg.state_reason, cfg.state_changed_at = new_state, reason or None, utcnow()
    cfg.state_changed_by_user_id = actor.id if actor else None
    cancelled = 0
    if new_state == "emergency_stopped":
        cancelled = emergency_cancel(db)
    log_activity(db, "agent.state_change", "ok", detail=reason, from_state=old, to_state=new_state, data={"cancelled_messages": cancelled})
    audit.record(db, "agent.state_change", actor=actor, details={"from": old, "to": new_state, "reason": reason, "cancelled": cancelled})
    return cfg


def emergency_cancel(db: Session) -> int:
    """Cancel every outbound message that has not been sent and every queued outbound job."""
    res = db.execute(
        update(OutreachMessage)
        .where(OutreachMessage.status.in_(("draft", "pending_approval", "approved", "queued")))
        .values(status="cancelled", blocked_reason="emergency stop")
    )
    db.execute(update(Job).where(Job.status == "queued", Job.kind.like("agent.%")).values(status="cancelled", last_error="emergency stop"))
    return res.rowcount or 0


def authorize_live_mode(db: Session, actor: User, enable: bool) -> AgentConfig:
    if not actor.is_admin:
        raise AgentError("Only an admin can authorize live operation")
    cfg = get_config(db)
    s = get_settings()
    if enable:
        problems = live_mode_blockers()
        if problems:
            raise AgentError("Live mode blocked: " + "; ".join(problems))
    cfg.live_mode = enable
    cfg.live_authorized_by_user_id = actor.id if enable else None
    cfg.live_authorized_at = utcnow() if enable else None
    log_activity(db, "agent.live_mode", "ok", detail=f"live_mode={enable} (email provider {s.email_provider})")
    audit.record(db, "agent.live_mode", actor=actor, details={"enabled": enable})
    return cfg


def live_mode_blockers() -> list[str]:
    s = get_settings()
    problems = []
    if s.email_provider == "sandbox":
        problems.append("EMAIL_PROVIDER is sandbox; configure an authorized provider")
    if not s.company_postal_address.strip():
        problems.append("COMPANY_POSTAL_ADDRESS is required in commercial email (CAN-SPAM)")
    if not s.outreach_from_address:
        problems.append("OUTREACH_FROM_ADDRESS is not set")
    if not s.base_url.startswith("https://"):
        problems.append("BASE_URL must be https so unsubscribe links work")
    return problems


def set_campaign_status(db: Session, campaign: Campaign, status: str, actor: User) -> None:
    if status not in ("draft", "active", "paused", "archived"):
        raise AgentError("Invalid campaign status")
    if status == "active" and not campaign.steps:
        raise AgentError("Campaign has no steps")
    old = campaign.status
    campaign.status = status
    if status in ("paused", "archived"):
        db.execute(
            update(OutreachMessage)
            .where(OutreachMessage.status.in_(("approved", "queued")), OutreachMessage.enrollment_id.in_(
                select_enrollment_ids(campaign.id)))
            .values(status="pending_approval", requires_approval_reason=f"campaign {status}")
        )
    log_activity(db, "campaign.status", "ok", campaign_id=campaign.id, from_state=old, to_state=status)
    audit.record(db, "campaign.status", actor=actor, entity=campaign, details={"from": old, "to": status})


def select_enrollment_ids(campaign_id: int):
    from app.models.outreach import Enrollment

    return select(Enrollment.id).where(Enrollment.campaign_id == campaign_id)

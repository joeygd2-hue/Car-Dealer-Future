"""The outreach gate. `check_send()` runs immediately before EVERY outbound prospect message.

It enforces: agent state, campaign state, enabled channel, approval, suppression
list (email, domain, phone), prospect stop conditions, sequence state, operating
schedule, global/hourly/per-domain sending limits, daily budget, and the
CAN-SPAM content requirements (postal address + working opt-out).

Legal note: these controls implement the CAN-SPAM Act's core requirements for
commercial email (no deceptive headers/subjects, identification, valid
physical postal address, a working opt-out honoured promptly, no sending
after opt-out). Only email is enabled. SMS and phone outreach are NOT
implemented because TCPA/Texas telephone-solicitation rules require consent
records this system does not collect. This is not legal advice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.base import utcnow
from app.models.costs import UsageRecord
from app.models.outreach import STOP_STAGES, AgentConfig, Enrollment, OutreachMessage, Prospect, Suppression


@dataclass
class GateResult:
    ok: bool
    reason: str | None = None
    retry_later: bool = False  # limit/schedule blocks are temporary; suppression is permanent


def email_domain(address: str | None) -> str:
    return (address or "").rsplit("@", 1)[-1].lower().strip()


def normalize_phone(phone: str | None) -> str:
    import re

    return re.sub(r"\D", "", phone or "")[-10:]


def is_suppressed(db: Session, *, email: str | None = None, phone: str | None = None) -> str | None:
    checks = []
    if email:
        checks += [("email", email.lower().strip()), ("domain", email_domain(email))]
    if phone:
        checks.append(("phone", normalize_phone(phone)))
    for kind, value in checks:
        row = db.scalar(select(Suppression).where(Suppression.kind == kind, Suppression.value == value))
        if row:
            return f"{kind} suppressed ({row.reason})"
    return None


def add_suppression(db: Session, kind: str, value: str, reason: str, source: str, actor_id: int | None = None) -> Suppression:
    value = value.lower().strip() if kind != "phone" else normalize_phone(value)
    existing = db.scalar(select(Suppression).where(Suppression.kind == kind, Suppression.value == value))
    if existing:
        return existing
    row = Suppression(kind=kind, value=value, reason=reason, source=source, created_by_user_id=actor_id)
    db.add(row)
    db.flush()
    return row


def in_send_window(cfg: AgentConfig, now_utc: datetime | None = None) -> bool:
    now_utc = now_utc or utcnow()
    local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(cfg.timezone))
    return local.weekday() in cfg.send_days and cfg.send_start_hour <= local.hour < cfg.send_end_hour


def sent_since(db: Session, since: datetime, domain: str | None = None) -> int:
    stmt = select(func.count()).select_from(OutreachMessage).where(OutreachMessage.status == "sent", OutreachMessage.sent_at >= since)
    if domain:
        stmt = stmt.where(OutreachMessage.to_address.like(f"%@{domain}"))
    return int(db.scalar(stmt) or 0)


def agent_spend_today_micros(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    start = datetime(now.year, now.month, now.day)
    return int(
        db.scalar(
            select(func.coalesce(func.sum(UsageRecord.cost_micros), 0)).where(
                UsageRecord.occurred_at >= start, UsageRecord.feature.like("agent%")
            )
        )
        or 0
    )


def budget_ok(db: Session, cfg: AgentConfig) -> bool:
    return agent_spend_today_micros(db) < cfg.daily_budget_micros


def check_send(db: Session, cfg: AgentConfig, msg: OutreachMessage, now: datetime | None = None) -> GateResult:
    now = now or utcnow()
    s = get_settings()
    if cfg.state != "running":
        return GateResult(False, f"agent is {cfg.state}", retry_later=cfg.state == "paused")
    if msg.channel not in cfg.enabled_channels or msg.channel != "email":
        return GateResult(False, f"channel '{msg.channel}' is not authorized")
    if msg.status != "approved":
        return GateResult(False, "message is not approved")
    prospect = db.get(Prospect, msg.prospect_id)
    if prospect is None:
        return GateResult(False, "prospect missing")
    if not msg.to_address:
        return GateResult(False, "no verified recipient address")
    if msg.kind != "reply" and prospect.stage in STOP_STAGES:
        return GateResult(False, f"prospect stage '{prospect.stage}' is a stop condition")
    if prospect.stage == "opted_out":
        return GateResult(False, "prospect opted out")
    reason = is_suppressed(db, email=msg.to_address, phone=prospect.phone)
    if reason:
        return GateResult(False, reason)
    if msg.enrollment_id and msg.kind != "reply":
        enr = db.get(Enrollment, msg.enrollment_id)
        if enr is None or enr.status != "active":
            return GateResult(False, f"sequence {enr.stop_reason if enr else 'missing'}")
        if enr.campaign.status != "active":
            return GateResult(False, f"campaign is {enr.campaign.status}", retry_later=enr.campaign.status == "paused")
    if msg.kind != "reply" and not in_send_window(cfg, now):
        return GateResult(False, "outside operating schedule", retry_later=True)
    day_start = now - timedelta(hours=24)
    if sent_since(db, day_start) >= cfg.daily_send_limit:
        return GateResult(False, "daily sending limit reached", retry_later=True)
    if sent_since(db, now - timedelta(hours=1)) >= cfg.hourly_send_limit:
        return GateResult(False, "hourly sending limit reached", retry_later=True)
    if msg.kind != "reply" and sent_since(db, day_start, email_domain(msg.to_address)) >= cfg.per_domain_daily_limit:
        return GateResult(False, "per-domain daily limit reached", retry_later=True)
    if not budget_ok(db, cfg):
        return GateResult(False, "daily agent budget exhausted", retry_later=True)
    # CAN-SPAM content requirements
    if cfg.live_mode and not s.company_postal_address.strip():
        return GateResult(False, "postal address missing (CAN-SPAM)")
    if "/unsubscribe/" not in msg.body:
        return GateResult(False, "message has no opt-out link")
    if msg.kind != "reply" and s.company_name.lower() not in msg.body.lower():
        return GateResult(False, "message does not identify the sender")
    return GateResult(True)

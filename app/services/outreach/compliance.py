"""Pre-send compliance gate for outreach. Called immediately before every send attempt.

Reasons are classified so the caller knows whether to wait (window/limits/budget/paused) or stop
(suppression, opt-out, stop conditions, unsupported channel, missing legal requirements).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import Campaign, CostEntry, Enrollment, OutreachAgentState, OutreachMessage, Prospect, Suppression
from app.services import controls

# Only email is permitted for cold B2B outreach (TCPA / Texas SB 140 make cold SMS/calls unsafe).
ALLOWED_COLD_CHANNELS = {"EMAIL"}
SENT_STATUSES = ("SENT", "SIMULATED")


def normalize_email(email: str | None) -> str | None:
    return email.strip().lower() if email and "@" in email else None


def email_domain(email: str | None) -> str | None:
    e = normalize_email(email)
    return e.split("@", 1)[1] if e else None


def normalize_domain(url_or_domain: str | None) -> str | None:
    if not url_or_domain:
        return None
    d = re.sub(r"^https?://", "", url_or_domain.strip().lower()).split("/")[0].split(":")[0]
    return d.removeprefix("www.") or None


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else None


def is_suppressed(db: Session, *, email: str | None = None, domain: str | None = None, phone: str | None = None) -> str | None:
    checks = [("EMAIL", normalize_email(email)), ("DOMAIN", normalize_domain(domain) or email_domain(email)),
              ("PHONE", normalize_phone(phone))]
    for kind, value in checks:
        if value:
            row = db.scalar(select(Suppression).where(Suppression.kind == kind, Suppression.value == value))
            if row:
                return f"{kind.lower()} suppressed ({row.reason})"
    return None


def add_suppression(db: Session, kind: str, value: str | None, reason: str, source: str) -> None:
    norm = {"EMAIL": normalize_email, "DOMAIN": normalize_domain, "PHONE": normalize_phone}[kind](value)
    if norm and not db.scalar(select(Suppression.id).where(Suppression.kind == kind, Suppression.value == norm)):
        db.add(Suppression(kind=kind, value=norm, reason=reason, source=source))
        db.flush()


def local_now(state: OutreachAgentState, now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(state.timezone)).replace(tzinfo=None)


def in_send_window(state: OutreachAgentState, now: datetime | None = None) -> bool:
    loc = local_now(state, now)
    return loc.weekday() in (state.send_weekdays or []) and state.send_window_start_hour <= loc.hour < state.send_window_end_hour


def seconds_until_window(state: OutreachAgentState, now: datetime | None = None) -> float:
    loc = local_now(state, now)
    for days in range(0, 8):
        day = (loc + timedelta(days=days)).replace(hour=state.send_window_start_hour, minute=0, second=0, microsecond=0)
        if day.weekday() in (state.send_weekdays or []) and (day > loc or (days == 0 and in_send_window(state, now))):
            return max(60.0, (day - loc).total_seconds())
    return 3600.0


def spent_today_cents(db: Session, state: OutreachAgentState, campaign_id: int | None = None) -> float:
    loc = local_now(state)
    start_local = loc.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.replace(tzinfo=ZoneInfo(state.timezone)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    stmt = select(func.coalesce(func.sum(CostEntry.amount_cents), 0.0)).where(
        CostEntry.scope == "OUTREACH", CostEntry.created_at >= start_utc)
    if campaign_id:
        stmt = stmt.where(CostEntry.campaign_id == campaign_id)
    return float(db.scalar(stmt) or 0.0)


def sends_since(db: Session, since: datetime, campaign_id: int | None = None) -> int:
    stmt = select(func.count(OutreachMessage.id)).where(OutreachMessage.status.in_(SENT_STATUSES),
                                                        OutreachMessage.kind == "SEQUENCE",
                                                        OutreachMessage.sent_at >= since)
    if campaign_id:
        stmt = stmt.where(OutreachMessage.campaign_id == campaign_id)
    return int(db.scalar(stmt) or 0)


@dataclass
class ComplianceResult:
    stop: list[str] = field(default_factory=list)  # permanent: never send this message
    wait: list[str] = field(default_factory=list)  # temporary: try again later

    @property
    def ok(self) -> bool:
        return not self.stop and not self.wait


def check_message(db: Session, msg: OutreachMessage, state: OutreachAgentState, *, live: bool,
                  estimated_cost_cents: float = 0.0, now: datetime | None = None) -> ComplianceResult:
    now = now or utcnow()
    r = ComplianceResult()
    settings = get_settings()
    prospect = db.get(Prospect, msg.prospect_id)
    # Global controls.
    if controls.get_flag(db, controls.OUTREACH_EMERGENCY_STOP) or state.state == "EMERGENCY_STOPPED":
        r.stop.append("emergency stop is active")
    elif state.state != "RUNNING":
        r.wait.append(f"agent is {state.state}")
    # Channel.
    if msg.channel not in ALLOWED_COLD_CHANNELS:
        r.stop.append(f"channel {msg.channel} is not authorized for outreach")
    # Approval.
    if msg.status not in ("APPROVED", "SENDING"):
        r.stop.append(f"message status {msg.status} is not approved for sending")
    # Prospect state / stop conditions.
    if prospect is None:
        r.stop.append("prospect missing")
        return r
    if prospect.status in ("DO_NOT_CONTACT", "CONVERTED", "DISQUALIFIED"):
        r.stop.append(f"prospect status {prospect.status}")
    if msg.kind == "SEQUENCE" and prospect.status in ("REPLIED", "MEETING_BOOKED"):
        r.stop.append(f"prospect {prospect.status.lower().replace('_', ' ')}; sequence stopped")
    if msg.enrollment_id:
        enr = db.get(Enrollment, msg.enrollment_id)
        if enr is None or enr.status != "ACTIVE":
            r.stop.append(f"enrollment {enr.status if enr else 'missing'}{' (' + enr.stop_reason + ')' if enr and enr.stop_reason else ''}")
        camp = db.get(Campaign, msg.campaign_id) if msg.campaign_id else None
        if camp is None or camp.status != "ACTIVE":
            r.wait.append("campaign is not active")
    supp = is_suppressed(db, email=msg.to_address, domain=prospect.domain, phone=prospect.phone)
    if supp:
        r.stop.append(supp)
    # Never contact the same address twice for the same step (duplicate prevention beyond idempotency keys).
    dup = db.scalar(select(OutreachMessage.id).where(
        OutreachMessage.id != msg.id, OutreachMessage.to_address == msg.to_address,
        OutreachMessage.campaign_id == msg.campaign_id, OutreachMessage.step_index == msg.step_index,
        OutreachMessage.kind == msg.kind, OutreachMessage.status.in_(SENT_STATUSES)))
    if dup and msg.kind == "SEQUENCE":
        r.stop.append("duplicate: this step was already sent to this address")
    # Live-only requirements.
    if live:
        if prospect.is_simulated:
            r.stop.append("simulated prospect cannot be contacted in live mode")
        if not prospect.email_verified_by_human:
            r.stop.append("recipient email not verified by a human as a published business address")
        if not (settings.outreach_postal_address and settings.outreach_sender_email and settings.outreach_company_name):
            r.stop.append("CAN-SPAM sender identity/postal address not configured")
    # Content requirements (CAN-SPAM + Texas ch. 321).
    if msg.kind == "SEQUENCE":
        if "unsubscribe" not in msg.body.lower():
            r.stop.append("missing unsubscribe instructions")
        if not msg.subject.startswith("ADV:"):
            r.stop.append("missing ADV: subject prefix (Texas Bus. & Com. Code ch. 321)")
    # Schedule, limits, budget (temporary).
    if msg.kind == "SEQUENCE":
        if not in_send_window(state, now):
            r.wait.append("outside operating schedule")
        if sends_since(db, now - timedelta(hours=1)) >= state.max_sends_per_hour:
            r.wait.append("hourly send limit reached")
        if sends_since(db, now - timedelta(hours=24)) >= state.max_sends_per_day:
            r.wait.append("daily send limit reached")
        if msg.campaign_id:
            camp = db.get(Campaign, msg.campaign_id)
            if camp and sends_since(db, now - timedelta(hours=24), camp.id) >= camp.daily_send_limit:
                r.wait.append("campaign daily send limit reached")
            if camp and spent_today_cents(db, state, camp.id) + estimated_cost_cents > camp.daily_budget_cents:
                r.wait.append("campaign daily budget reached")
    if spent_today_cents(db, state) + estimated_cost_cents > state.daily_budget_cents:
        r.wait.append("daily budget reached")
    return r

"""Compliance and safety gates evaluated immediately before every outreach action.

Every check is recorded on the message (policy_checks) so the audit trail shows
why something was sent or blocked. Sources: CAN-SPAM (FTC guide), Gmail/Yahoo
sender rules, TCPA / Texas Bus. & Com. Code ch. 302/304 (SMS/voice disabled),
LinkedIn User Agreement (no automation) - see
docs/research/outreach-providers-and-compliance-2026-10.md.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.jobs import get_flag
from app.core.time import utcnow
from app.models import Campaign, CostEntry, Enrollment, OutreachMessage, PlatformIntegration, Prospect, ProspectContact, Suppression

GLOBAL_PAUSE = "outreach.global_pause"
EMERGENCY_STOP = "outreach.emergency_stop"
ALLOWED_CHANNELS = ("email",)  # SMS/voice require documented written consent + legal sign-off; not enabled.
STOP_STATUSES = ("do_not_contact", "won", "lost", "disqualified")
SEQUENCE_STOP_STATUSES = STOP_STATUSES + ("replied", "meeting_booked")
BANNED_CLAIMS = [
    r"guarantee[ds]?", r"double your (sales|leads)", r"\d+\s?% (more|increase)", r"risk[- ]free", r"no obligation",
    r"act now", r"limited time", r"last chance", r"we (already )?work with (your|other) (competitors|dealers in)",
    r"as (we )?discussed", r"following up on our (call|conversation)", r"per our conversation",
]
DECEPTIVE_SUBJECT = re.compile(r"^\s*(re|fwd?|fw)\s*:", re.I)


def unsubscribe_token(message_id: str) -> str:
    key = (settings.unsubscribe_secret or settings.encryption_key or "dev-unsubscribe-secret").encode()
    return message_id + "." + hmac.new(key, message_id.encode(), hashlib.sha256).hexdigest()[:32]


def verify_unsubscribe_token(token: str) -> str | None:
    if "." not in token:
        return None
    mid = token.rsplit(".", 1)[0]
    return mid if hmac.compare_digest(unsubscribe_token(mid), token) else None


def unsubscribe_url(message_id: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/u/{unsubscribe_token(message_id)}"


def is_suppressed(db: Session, email: str | None, now: datetime | None = None) -> Suppression | None:
    if not email:
        return None
    now = now or utcnow()
    email = email.lower().strip()
    domain = email.split("@")[-1]
    rows = db.scalars(select(Suppression).where(
        ((Suppression.kind == "email") & (Suppression.value == email)) | ((Suppression.kind == "domain") & (Suppression.value == domain)))).all()
    return next((r for r in rows if r.expires_at is None or r.expires_at > now), None)


def suppress(db: Session, kind: str, value: str, reason: str, source: str, by: str | None = None,
             expires_at: datetime | None = None) -> Suppression:
    value = value.lower().strip()
    s = db.scalar(select(Suppression).where(Suppression.kind == kind, Suppression.value == value))
    if s:
        if reason in ("opt_out", "complaint", "legal", "bounce") or s.expires_at is not None:
            s.reason, s.expires_at = reason, expires_at
        return s
    s = Suppression(kind=kind, value=value, reason=reason, source=source, created_by=by, expires_at=expires_at)
    db.add(s)
    db.flush()
    return s


def within_schedule(campaign: Campaign, now: datetime) -> tuple[bool, str]:
    sch = campaign.operating_schedule or {}
    tz = ZoneInfo(sch.get("timezone", "America/Chicago"))
    local = now.astimezone(tz)
    days = sch.get("days", [0, 1, 2, 3, 4])
    start, end = sch.get("start_hour", 9), sch.get("end_hour", 17)
    ok = local.weekday() in days and start <= local.hour < end
    return ok, f"local time {local.strftime('%a %H:%M %Z')}, window {start}:00-{end}:00 days {days}"


def next_window_start(campaign: Campaign, now: datetime) -> datetime:
    sch = campaign.operating_schedule or {}
    tz = ZoneInfo(sch.get("timezone", "America/Chicago"))
    days, start = sch.get("days", [0, 1, 2, 3, 4]), sch.get("start_hour", 9)
    local = now.astimezone(tz)
    for i in range(0, 8):
        d = (local + timedelta(days=i)).replace(hour=start, minute=0, second=0, microsecond=0)
        if d.weekday() in days and d > local:
            return d.astimezone(ZoneInfo("UTC"))
    return now + timedelta(days=1)


def sent_count(db: Session, *, since: datetime, campaign_id: str | None = None, integration_id: str | None = None) -> int:
    q = select(func.count()).select_from(OutreachMessage).where(OutreachMessage.direction == "outbound",
                                                                 OutreachMessage.status.in_(("sent", "sending")),
                                                                 OutreachMessage.sent_at >= since)
    if campaign_id:
        q = q.join(Enrollment, Enrollment.id == OutreachMessage.enrollment_id).where(Enrollment.campaign_id == campaign_id)
    if integration_id:
        q = q.where(OutreachMessage.integration_id == integration_id)
    return db.scalar(q) or 0


def spend_today(db: Session, campaign_id: str | None, now: datetime) -> float:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(CostEntry.created_at >= start)
    if campaign_id:
        q = q.where(CostEntry.campaign_id == campaign_id)
    return float(db.scalar(q) or 0.0)


def content_checks(subject: str, body: str, *, first_touch: bool, postal_address: str, unsub_url_present: bool) -> list[dict]:
    checks = []
    low = body.lower()
    checks.append(_c("postal_address", bool(postal_address) and postal_address in body, "CAN-SPAM: valid physical postal address in message"))
    checks.append(_c("opt_out_mechanism", unsub_url_present and ("unsubscribe" in low or "opt out" in low),
                     "CAN-SPAM: clear opt-out instructions and working link"))
    checks.append(_c("subject_not_deceptive", not (first_touch and DECEPTIVE_SUBJECT.search(subject)) and bool(subject.strip()),
                     "No fake 'Re:'/'Fwd:' on a first message; subject present"))
    hits = [p for p in BANNED_CLAIMS if re.search(p, low)]
    checks.append(_c("no_unsupported_claims", not hits, f"Banned/unsupported claims: {hits}" if hits else "none"))
    checks.append(_c("length", 40 <= len(body) <= 4000, f"{len(body)} chars"))
    return checks


def _c(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def evaluate_send(db: Session, msg: OutboundMessageLike, *, campaign: Campaign | None, enrollment: Enrollment | None,
                  prospect: Prospect, contact: ProspectContact | None, integration: PlatformIntegration,
                  now: datetime | None = None, estimated_cost: float = 0.0) -> list[dict]:
    """Return the list of checks. The message may be sent only if every check passed."""
    now = now or utcnow()
    checks: list[dict] = []
    checks.append(_c("emergency_stop_off", not get_flag(db, EMERGENCY_STOP, False), "global emergency stop"))
    checks.append(_c("global_pause_off", not get_flag(db, GLOBAL_PAUSE, False), "global pause"))
    if campaign is not None:
        checks.append(_c("campaign_active", campaign.status == "active", f"campaign status {campaign.status}"))
        checks.append(_c("channel_allowed", campaign.channel in ALLOWED_CHANNELS, f"channel {campaign.channel}"))
    is_sequence = msg.kind == "sequence"
    stop_set = SEQUENCE_STOP_STATUSES if is_sequence else STOP_STATUSES
    checks.append(_c("prospect_contactable", prospect.status not in stop_set, f"prospect status {prospect.status}"))
    if enrollment is not None and is_sequence:
        checks.append(_c("enrollment_active", enrollment.state not in ("stopped", "completed", "replied", "meeting_booked"),
                         f"enrollment state {enrollment.state}"))
        if campaign is not None:
            checks.append(_c("followup_limit", (msg.step or 1) <= 1 + campaign.max_followups,
                             f"step {msg.step} of max {1 + campaign.max_followups}"))
    to = msg.to_address or (contact.email if contact else None)
    sup = is_suppressed(db, to, now)
    checks.append(_c("not_suppressed", sup is None, f"suppressed ({sup.reason})" if sup else "clear"))
    checks.append(_c("contact_valid", bool(to) and (contact is None or contact.email_status not in ("bounced", "invalid")),
                     f"recipient {to}"))
    checks.append(_c("contact_has_evidence", contact is None or bool(contact.evidence_url),
                     "recipient address was published by the business (evidence stored)"))
    checks.append(_c("approved", (not msg.approval_required) or bool(msg.approved_by), "human approval" if msg.approval_required else "routine/auto"))
    if campaign is not None:
        ok, detail = within_schedule(campaign, now)
        checks.append(_c("operating_schedule", ok, detail))
        hour = sent_count(db, since=now - timedelta(hours=1), campaign_id=campaign.id)
        day = sent_count(db, since=now - timedelta(hours=24), campaign_id=campaign.id)
        checks.append(_c("campaign_hourly_limit", hour < campaign.hourly_send_limit, f"{hour}/{campaign.hourly_send_limit} in last hour"))
        checks.append(_c("campaign_daily_limit", day < campaign.daily_send_limit, f"{day}/{campaign.daily_send_limit} in last 24h"))
        spent = spend_today(db, campaign.id, now)
        checks.append(_c("daily_budget", spent + estimated_cost <= campaign.daily_budget_usd,
                         f"${spent:.2f} spent + ${estimated_cost:.2f} est. vs ${campaign.daily_budget_usd:.2f}"))
    cap = int((integration.config or {}).get("daily_cap", 50))
    used = sent_count(db, since=now - timedelta(hours=24), integration_id=integration.id)
    checks.append(_c("mailbox_daily_cap", used < cap, f"{used}/{cap} from this mailbox in 24h"))
    checks.append(_c("integration_healthy", integration.health_status != "down", f"integration health {integration.health_status}"))
    live = integration.provider != "sandbox"
    if live:
        checks.append(_c("live_allowed_env", settings.outreach_live_allowed, "OUTREACH_LIVE_ALLOWED"))
        checks.append(_c("integration_live_authorized", integration.live_authorized, "platform admin authorised this mailbox for live use"))
        if campaign is not None:
            checks.append(_c("campaign_live_authorized", campaign.live_authorized, "platform admin authorised this campaign for live sending"))
        checks.append(_c("not_simulated_prospect", not prospect.is_simulated, "simulated prospects are sandbox-only"))
    if campaign is not None:
        checks += content_checks(msg.subject, msg.body, first_touch=(msg.step == 1 and is_sequence),
                                 postal_address=campaign.postal_address, unsub_url_present="/u/" in msg.body)
    return checks


def all_passed(checks: list[dict]) -> bool:
    return all(c["passed"] for c in checks)


def failed(checks: list[dict]) -> list[str]:
    return [c["check"] for c in checks if not c["passed"]]


# Hard blockers mean "never send this"; soft ones mean "not now" (reschedule).
SOFT = {"operating_schedule", "campaign_hourly_limit", "campaign_daily_limit", "daily_budget", "mailbox_daily_cap",
        "integration_healthy", "global_pause_off"}


OutboundMessageLike = OutreachMessage

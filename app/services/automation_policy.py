"""Decide whether a generated reply may be sent automatically or must wait for staff review.

Draft-only (Mode A) is the default. Every auto-send requires ALL checks to pass; every reason a
reply was held is recorded on the draft for transparency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.integrations.registry import META_MESSENGER, SENDABLE_CHANNELS
from app.models import (
    MODE_CONTROLLED,
    MODE_DRAFT_ONLY,
    MODE_FAQ,
    AutomationSettings,
    ContactSuppression,
    Conversation,
    Dealership,
    EvaluationRun,
    IntegrationConnection,
    Message,
    ModeAuthorization,
)
from app.services import controls
from app.services.responder import analysis as A
from app.services.responder.engine import ENGINE_VERSION, ReplyPlan, within_hours
from app.services.responder.facts import FactSheet

# Mode B may automate only these low-risk intents, and only via approved templates/FAQs.
FAQ_AUTO_INTENTS = {A.GREETING, A.THANKS, A.AVAILABILITY, A.PRICE, A.MILEAGE, A.FEATURES, A.HOURS, A.LOCATION, "faq"}
# Mode C additionally allows dealership-approved intents from this list (never escalation topics).
CONTROLLABLE_INTENTS = {A.TEST_DRIVE, A.CONTACT_PROVIDED, A.VEHICLE_DETAILS}
EVALUATION_MAX_AGE_DAYS = 30


@dataclass
class Decision:
    auto_send: bool
    reasons: list[str] = field(default_factory=list)


def get_settings_row(db: Session, dealership_id: int) -> AutomationSettings:
    row = db.scalar(select(AutomationSettings).where(AutomationSettings.dealership_id == dealership_id))
    if row is None:
        row = AutomationSettings(dealership_id=dealership_id, mode=MODE_DRAFT_ONLY)
        db.add(row)
        db.flush()
    return row


def valid_mode_authorization(db: Session, dealership_id: int, mode: str) -> ModeAuthorization | None:
    """An unrevoked dealership authorization tied to a passing evaluation of the current engine."""
    cutoff = utcnow() - timedelta(days=EVALUATION_MAX_AGE_DAYS)
    for auth in db.scalars(select(ModeAuthorization).where(
            ModeAuthorization.dealership_id == dealership_id, ModeAuthorization.mode == mode,
            ModeAuthorization.revoked_at.is_(None)).order_by(ModeAuthorization.id.desc())):
        run = db.get(EvaluationRun, auth.evaluation_run_id)
        if run and run.passed and run.engine_version == ENGINE_VERSION and run.created_at >= cutoff:
            return auth
    return None


def effective_mode(db: Session, settings: AutomationSettings) -> str:
    if settings.mode == MODE_DRAFT_ONLY:
        return MODE_DRAFT_ONLY
    return settings.mode if valid_mode_authorization(db, settings.dealership_id, settings.mode) else MODE_DRAFT_ONLY


def channel_connection(db: Session, dealership_id: int, channel: str) -> IntegrationConnection | None:
    return db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.dealership_id == dealership_id, IntegrationConnection.provider == channel))


def automation_blockers(db: Session, conv: Conversation, settings: AutomationSettings) -> list[str]:
    """Conditions that stop ANY automated message on this conversation (pause/takeover/opt-out)."""
    reasons = []
    if controls.get_flag(db, controls.GLOBAL_AUTOMATION_PAUSE):
        reasons.append("global automation pause is on")
    if settings.paused:
        reasons.append("dealership automation is paused")
    conn = channel_connection(db, conv.dealership_id, conv.channel)
    if conn is not None and conn.paused:
        reasons.append(f"channel {conv.channel} is paused")
    if conv.human_takeover:
        reasons.append("a staff member has taken over this conversation")
    if conv.human_requested:
        reasons.append("customer asked for a human")
    if conv.opted_out:
        reasons.append("customer opted out")
    if conv.contact_id is not None:
        suppressed = db.scalar(select(ContactSuppression.id).where(
            ContactSuppression.dealership_id == conv.dealership_id, ContactSuppression.channel == conv.channel,
            ContactSuppression.address == conv.external_conversation_id))
        if suppressed:
            reasons.append("recipient is on the dealership opt-out list")
    return reasons


def decide(db: Session, conv: Conversation, plan: ReplyPlan, fs: FactSheet, now: datetime | None = None) -> Decision:
    now = now or utcnow()
    settings = get_settings_row(db, conv.dealership_id)
    reasons: list[str] = []
    if plan.no_reply:
        return Decision(False, ["no reply should be sent (opt-out)"])
    mode = effective_mode(db, settings)
    if mode == MODE_DRAFT_ONLY:
        reasons.append("draft-only mode (Mode A)" if settings.mode == MODE_DRAFT_ONLY
                       else f"{settings.mode} lacks a valid authorization + passing evaluation; using draft-only")
    reasons += automation_blockers(db, conv, settings)
    if conv.channel not in SENDABLE_CHANNELS:
        reasons.append(f"{conv.channel} replies are sent manually by staff")
    if plan.escalation_reasons:
        reasons.append("escalation required: " + ", ".join(plan.escalation_reasons))
    if plan.unresolved:
        reasons.append("unresolved questions: " + ", ".join(plan.unresolved))
    if plan.confidence < settings.confidence_threshold:
        reasons.append(f"confidence {plan.confidence:.2f} below threshold {settings.confidence_threshold:.2f}")
    if plan.validation_errors:
        reasons.append("output validation failed")
    if plan.appointment_request is not None:
        reasons.append("appointment requests are confirmed by staff")

    allowed = set(FAQ_AUTO_INTENTS)
    if mode == MODE_CONTROLLED:
        allowed |= set(settings.controlled_intents or []) & CONTROLLABLE_INTENTS
    substantive = [i for i in plan.intents if i not in (A.GREETING, A.THANKS)]
    outside = [i for i in plan.intents if i not in allowed]
    if outside:
        reasons.append("intents not approved for automation: " + ", ".join(outside))
    if not substantive and "faq" not in plan.template_covered:
        reasons.append("nothing substantive to answer")
    # Low-risk answers must come from approved templates/FAQs (Mode B rule; also applies in Mode C).
    uncovered = [i for i in plan.answered if i in FAQ_AUTO_INTENTS and i not in plan.template_covered]
    if uncovered:
        reasons.append("no approved auto-send template for: " + ", ".join(uncovered))
    else:
        for intent in plan.template_covered:
            tpl = fs.templates.get(intent)
            if intent != "faq" and tpl is not None and not tpl.auto_send_allowed:
                reasons.append(f"template for {intent} is not approved for auto-send")
        if "faq" in plan.template_covered:
            faq_ids = [int(f.split(":")[1]) for f in plan.facts_used if f.startswith("faq:")]
            if any(not f.auto_send_allowed for f in fs.faqs if f.id in faq_ids):
                reasons.append("FAQ answer is not approved for auto-send")

    # Business hours.
    dealership = db.get(Dealership, conv.dealership_id)
    local_now = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(dealership.timezone)).replace(tzinfo=None)
    if not settings.auto_reply_outside_business_hours and within_hours(fs, local_now, 0) is not True:
        reasons.append("outside business hours (or hours not configured)")

    # Messaging window (Messenger standard 24h policy).
    if conv.channel == META_MESSENGER and (conv.last_inbound_at is None or now - conv.last_inbound_at > timedelta(hours=24)):
        reasons.append("outside Messenger 24-hour window")

    # Rate limits.
    hour_ago = now - timedelta(hours=1)
    conv_count = db.scalar(select(func.count(Message.id)).where(
        Message.conversation_id == conv.id, Message.sender_type == "AI_AUTO", Message.created_at >= hour_ago)) or 0
    if conv_count >= settings.max_auto_replies_per_conversation_per_hour:
        reasons.append("conversation auto-reply rate limit reached")
    dealer_count = db.scalar(select(func.count(Message.id)).where(
        Message.dealership_id == conv.dealership_id, Message.sender_type == "AI_AUTO", Message.created_at >= hour_ago)) or 0
    if dealer_count >= settings.max_auto_replies_per_dealership_per_hour:
        reasons.append("dealership auto-reply rate limit reached")
    last_auto = db.scalar(select(func.max(Message.created_at)).where(
        Message.conversation_id == conv.id, Message.sender_type == "AI_AUTO"))
    if last_auto and (now - last_auto).total_seconds() < settings.min_seconds_between_auto_replies:
        reasons.append("minimum interval between auto-replies not reached")

    return Decision(auto_send=not reasons, reasons=reasons)

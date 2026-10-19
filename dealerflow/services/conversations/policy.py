"""Decide whether a generated reply may be sent automatically or must wait for staff review (ported from Phase 3).

Draft-only (mode A) is the default. An automatic send needs ALL of:
  * mode B/C with an unrevoked dealership authorization tied to a passing evaluation of the current engine;
  * the Phase 4 onboarding gate ``inquiry_auto_response`` enabled for the client (its prerequisites include a
    verified ``customer_messaging`` connection);
  * no pause (platform, dealership, channel), no takeover, no opt-out, no emergency stop;
  * an approved auto-send template/FAQ for every answered intent, no escalation, no unresolved question,
    confidence above threshold, inside business hours, and within the rate limits.
Every reason a reply was held is recorded on the draft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import (
    AgentState,
    Client,
    Conversation,
    ConversationMessage,
    CustomerSuppression,
    IntegrationConnection,
    ResponseAutomationSettings,
    ResponseEvaluationRun,
    ResponseModeAuthorization,
)
from ...models_conversations import MODE_CONTROLLED, MODE_DRAFT_ONLY, SENDABLE_CHANNELS
from .. import onboarding
from ..settings_store import get_setting
from .common import local_now
from .responder import analysis as A
from .responder.engine import ENGINE_VERSION, ReplyPlan, within_hours
from .responder.facts import FactSheet

# Mode B may automate only these low-risk intents, and only via approved templates/FAQs.
FAQ_AUTO_INTENTS = {A.GREETING, A.THANKS, A.AVAILABILITY, A.PRICE, A.MILEAGE, A.FEATURES, A.HOURS, A.LOCATION, "faq"}
# Mode C additionally allows dealership-approved intents from this list (never escalation topics).
CONTROLLABLE_INTENTS = {A.TEST_DRIVE, A.CONTACT_PROVIDED, A.VEHICLE_DETAILS}
EVALUATION_MAX_AGE_DAYS = 30
GLOBAL_PAUSE_KEY = "conversations_global_pause"


@dataclass
class Decision:
    auto_send: bool
    reasons: list[str] = field(default_factory=list)


def settings_row(db: Session, client_id: int) -> ResponseAutomationSettings:
    row = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == client_id))
    if row is None:
        row = ResponseAutomationSettings(client_id=client_id, mode=MODE_DRAFT_ONLY)
        db.add(row)
        db.flush()
    return row


def global_pause(db: Session) -> bool:
    return bool((get_setting(db, GLOBAL_PAUSE_KEY) or {}).get("on"))


def emergency_stopped(db: Session) -> bool:
    st = db.get(AgentState, 1)
    return st is not None and st.status == "emergency_stopped"


def valid_mode_authorization(db: Session, client_id: int, mode: str) -> ResponseModeAuthorization | None:
    """An unrevoked dealership authorization tied to a passing evaluation of the current engine."""
    cutoff = utcnow() - timedelta(days=EVALUATION_MAX_AGE_DAYS)
    for auth in db.scalars(select(ResponseModeAuthorization).where(
            ResponseModeAuthorization.client_id == client_id, ResponseModeAuthorization.mode == mode,
            ResponseModeAuthorization.revoked_at.is_(None)).order_by(ResponseModeAuthorization.id.desc())):
        run = db.get(ResponseEvaluationRun, auth.evaluation_run_id)
        if run and run.passed and run.engine_version == ENGINE_VERSION and run.created_at >= cutoff:
            return auth
    return None


def effective_mode(db: Session, settings: ResponseAutomationSettings) -> str:
    if settings.mode == MODE_DRAFT_ONLY:
        return MODE_DRAFT_ONLY
    return settings.mode if valid_mode_authorization(db, settings.client_id, settings.mode) else MODE_DRAFT_ONLY


def channel_connection(db: Session, client_id: int, channel: str) -> IntegrationConnection | None:
    return db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.client_id == client_id, IntegrationConnection.kind == "customer_messaging",
        IntegrationConnection.provider == channel))


def is_suppressed(db: Session, conv: Conversation) -> bool:
    return db.scalar(select(CustomerSuppression.id).where(
        CustomerSuppression.client_id == conv.client_id, CustomerSuppression.channel == conv.channel,
        CustomerSuppression.address == conv.external_conversation_id)) is not None


def automation_blockers(db: Session, conv: Conversation, settings: ResponseAutomationSettings) -> list[str]:
    """Conditions that stop ANY automated message on this conversation."""
    reasons = []
    if emergency_stopped(db):
        reasons.append("platform emergency stop is active")
    if global_pause(db):
        reasons.append("platform-wide conversation automation pause is on")
    if settings.paused:
        reasons.append("dealership automation is paused")
    conn = channel_connection(db, conv.client_id, conv.channel)
    if conn is not None and conn.status == "disabled":
        reasons.append(f"channel {conv.channel} is paused")
    if conv.human_takeover:
        reasons.append("a staff member has taken over this conversation")
    if conv.human_requested:
        reasons.append("customer asked for a human")
    if conv.opted_out:
        reasons.append("customer opted out")
    if is_suppressed(db, conv):
        reasons.append("recipient is on the dealership opt-out list")
    return reasons


def decide(db: Session, conv: Conversation, plan: ReplyPlan, fs: FactSheet, now: datetime | None = None) -> Decision:
    now = now or utcnow()
    settings = settings_row(db, conv.client_id)
    reasons: list[str] = []
    if plan.no_reply:
        return Decision(False, ["no reply should be sent (opt-out)"])
    mode = effective_mode(db, settings)
    if mode == MODE_DRAFT_ONLY:
        reasons.append("draft-only mode (mode A)" if settings.mode == MODE_DRAFT_ONLY
                       else f"{settings.mode} lacks a valid authorization + passing evaluation; using draft-only")
    if not onboarding.automation_enabled(db, conv.client_id, "inquiry_auto_response"):
        reasons.append("onboarding gate 'inquiry_auto_response' is not enabled for this dealership")
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
    if not plan.answered:
        reasons.append("no question was answered from verified data (clarifying questions are sent by staff)")
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

    client = db.get(Client, conv.client_id)
    if not settings.auto_reply_outside_business_hours and within_hours(fs, local_now(client.timezone), 0) is not True:
        reasons.append("outside business hours (or hours not configured)")

    hour_ago = now - timedelta(hours=1)
    auto = (ConversationMessage.sender_type == "ai_auto", ConversationMessage.created_at >= hour_ago,
            ConversationMessage.client_id == conv.client_id)
    conv_count = db.scalar(select(func.count(ConversationMessage.id)).where(
        ConversationMessage.conversation_id == conv.id, *auto)) or 0
    if conv_count >= settings.max_auto_replies_per_conversation_per_hour:
        reasons.append("conversation auto-reply rate limit reached")
    dealer_count = db.scalar(select(func.count(ConversationMessage.id)).where(*auto)) or 0
    if dealer_count >= settings.max_auto_replies_per_dealership_per_hour:
        reasons.append("dealership auto-reply rate limit reached")
    last_auto = db.scalar(select(func.max(ConversationMessage.created_at)).where(
        ConversationMessage.client_id == conv.client_id, ConversationMessage.conversation_id == conv.id,
        ConversationMessage.sender_type == "ai_auto"))
    if last_auto and (now - last_auto).total_seconds() < settings.min_seconds_between_auto_replies:
        reasons.append("minimum interval between auto-replies not reached")
    return Decision(auto_send=not reasons, reasons=reasons)

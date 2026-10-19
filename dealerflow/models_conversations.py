"""Phase 3 models: dealership customer conversations, response drafts, escalations, test-drive appointments,
response automation, staff notifications and the outbound send ledger.

Ported from the Phase 3 ``app/`` package (branch ``claude/magical-darwin-nncp64``) onto the shared model:

- The tenant is ``clients`` (Phase 3 called it ``dealerships``). Every customer-facing table inherits
  ``TenantMixin`` and references other client-owned rows through composite ``(client_id, id)`` foreign keys,
  so the database refuses a conversation that points at another dealership's contact, vehicle or message.
- Vehicles are Phase 2's ``vehicles``; raw provider events use Phase 4's ``webhook_events``; the emergency stop
  is Phase 1's agent state. Phase 3's own copies of those tables were not ported.
- Renamed where a Phase 1/4 table already used the name: ``automation_settings`` (Phase 4 onboarding gates) →
  ``response_automation_settings``; ``approved_templates``/``faqs`` → ``response_templates``/``dealer_faqs``
  (distinct from Phase 1's outreach ``templates`` and ``knowledge_items``); ``messages`` → ``conversation_messages``.

Statuses keep Phase 3's upper-case values. Timestamps are naive UTC like the rest of dealerflow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow
from .models_dealer import TenantMixin

__all__ = [
    "MODE_DRAFT_ONLY", "MODE_FAQ", "MODE_CONTROLLED", "MODES",
    "CustomerContact", "CustomerIdentity", "CustomerLead", "ContactSuppression", "Conversation", "ConversationMessage",
    "ResponseDraft", "Escalation", "Appointment", "AppointmentEvent", "ResponseAutomationSettings", "ModeAuthorization",
    "ResponseTemplate", "DealerFAQ", "EvaluationRun", "Notification", "NotificationPreference", "RateCounter",
    "SendAttempt",
]

MODE_DRAFT_ONLY = "A_DRAFT_ONLY"
MODE_FAQ = "B_FAQ_AUTOMATION"
MODE_CONTROLLED = "C_CONTROLLED"
MODES = (MODE_DRAFT_ONLY, MODE_FAQ, MODE_CONTROLLED)


class _Stamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


def _tenant_fk(column: str, target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["client_id", column], [f"{target}.client_id", f"{target}.id"], name=name)


# --- Dealership customers (end customers of a client dealership; not outreach prospects) ------------------


class CustomerContact(TenantMixin, _Stamped, Base):
    __tablename__ = "customer_contacts"
    __table_args__ = (UniqueConstraint("client_id", "id", name="uq_customer_contacts_client_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    preferred_language: Mapped[str | None] = mapped_column(String(5))  # en|es
    contact_source: Mapped[str | None] = mapped_column(String(100))  # how the details were collected
    sms_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sms_consent_source: Mapped[str | None] = mapped_column(Text)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)

    identities: Mapped[list[CustomerIdentity]] = relationship(back_populates="contact", cascade="all, delete-orphan")


class CustomerIdentity(TenantMixin, Base):
    """Channel-specific identifier (Messenger PSID, phone number, chat visitor id...)."""

    __tablename__ = "customer_identities"
    __table_args__ = (UniqueConstraint("client_id", "channel", "external_id", name="uq_customer_identity"),
                      _tenant_fk("contact_id", "customer_contacts", "fk_customer_identities_contact"))

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)

    contact: Mapped[CustomerContact] = relationship(
        back_populates="identities", foreign_keys=[contact_id],
        primaryjoin="and_(CustomerIdentity.contact_id == CustomerContact.id, "
                    "CustomerIdentity.client_id == CustomerContact.client_id)")


class CustomerLead(TenantMixin, _Stamped, Base):
    """Sales opportunity in the dealership's pipeline, linked to a conversation when one exists."""

    __tablename__ = "customer_leads"
    __table_args__ = (UniqueConstraint("client_id", "id", name="uq_customer_leads_client_id"),
                      _tenant_fk("contact_id", "customer_contacts", "fk_customer_leads_contact"),
                      _tenant_fk("vehicle_id", "vehicles", "fk_customer_leads_vehicle"))

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(40), nullable=False)  # channel or ADF provider
    status: Mapped[str] = mapped_column(String(30), default="NEW", nullable=False)  # NEW|CONTACTED|APPOINTMENT|SOLD|LOST
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    external_ref: Mapped[str | None] = mapped_column(String(200))


class ContactSuppression(TenantMixin, Base):
    """Per-dealership opt-out registry for customer messaging (separate from outreach ``suppressions``)."""

    __tablename__ = "contact_suppressions"
    __table_args__ = (UniqueConstraint("client_id", "channel", "address", name="uq_contact_suppression"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    address: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), default="OPT_OUT", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --- Unified inbox ---------------------------------------------------------------------------------------


class Conversation(TenantMixin, _Stamped, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_conversations_client_id"),
        UniqueConstraint("client_id", "channel", "external_conversation_id", name="uq_conversation_thread"),
        _tenant_fk("contact_id", "customer_contacts", "fk_conversations_contact"),
        _tenant_fk("vehicle_id", "vehicles", "fk_conversations_vehicle"),
        _tenant_fk("lead_id", "customer_leads", "fk_conversations_lead"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)  # WEBSITE_CHAT|META_MESSENGER|TWILIO_SMS|ADF_LEAD|MARKETPLACE_ASSISTED
    external_conversation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    lead_id: Mapped[int | None] = mapped_column(Integer)
    preferred_language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)  # OPEN|AWAITING_STAFF|AWAITING_CUSTOMER|ESCALATED|CLOSED
    # SIMULATED conversations (staff simulator, demo dealerships) are never reported as real inquiries.
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    human_takeover: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    takeover_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    takeover_at: Mapped[datetime | None] = mapped_column(DateTime)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)
    human_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    unresolved_questions: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    next_action: Mapped[str | None] = mapped_column(Text)
    appointment_status: Mapped[str] = mapped_column(String(30), default="NONE", nullable=False)
    escalation_status: Mapped[str] = mapped_column(String(20), default="NONE", nullable=False)  # NONE|OPEN|RESOLVED
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    pii_purged_at: Mapped[datetime | None] = mapped_column(DateTime)

    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation", order_by="ConversationMessage.id", cascade="all, delete-orphan",
        foreign_keys="ConversationMessage.conversation_id",
        primaryjoin="and_(Conversation.id == ConversationMessage.conversation_id, "
                    "Conversation.client_id == ConversationMessage.client_id)")


class ConversationMessage(TenantMixin, Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_conversation_messages_client_id"),
        UniqueConstraint("channel", "provider_message_id", name="uq_conversation_message_provider_id"),
        _tenant_fk("conversation_id", "conversations", "fk_conversation_messages_conversation"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # INBOUND|OUTBOUND|NOTE
    sender_type: Mapped[str] = mapped_column(String(20), nullable=False)  # CUSTOMER|STAFF|AI_AUTO|SYSTEM
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(5))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # RECEIVED|QUEUED|SENDING|SENT|FAILED|SIMULATED|MANUAL_EXTERNAL|BLOCKED|UNCERTAIN (needs human reconciliation)
    delivery_status: Mapped[str] = mapped_column(String(20), default="RECEIVED", nullable=False)
    delivery_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    conversation: Mapped[Conversation] = relationship(
        back_populates="messages", foreign_keys=[conversation_id],
        primaryjoin="and_(Conversation.id == ConversationMessage.conversation_id, "
                    "Conversation.client_id == ConversationMessage.client_id)")


class ResponseDraft(TenantMixin, _Stamped, Base):
    __tablename__ = "response_drafts"
    __table_args__ = (
        _tenant_fk("conversation_id", "conversations", "fk_response_drafts_conversation"),
        _tenant_fk("in_reply_to_message_id", "conversation_messages", "fk_response_drafts_reply_to"),
        _tenant_fk("sent_message_id", "conversation_messages", "fk_response_drafts_sent_message"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    in_reply_to_message_id: Mapped[int | None] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    # PENDING_REVIEW|SENT|AUTO_SENT|REJECTED|SUPERSEDED|BLOCKED|SEND_FAILED
    status: Mapped[str] = mapped_column(String(20), default="PENDING_REVIEW", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    intents: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    facts_used: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    # Values of the verified facts at draft time; an automated send is blocked if any of them changed since.
    fact_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    unresolved: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    escalation_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(40))  # DRAFT_ONLY|AUTO_SEND|NO_REPLY
    decision_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    generated_by: Mapped[str] = mapped_column(String(40), default="template", nullable=False)  # template|template+llm
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_message_id: Mapped[int | None] = mapped_column(Integer)


class Escalation(TenantMixin, _Stamped, Base):
    __tablename__ = "escalations"
    __table_args__ = (_tenant_fk("conversation_id", "conversations", "fk_escalations_conversation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)  # OPEN|ACKNOWLEDGED|RESOLVED
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- Test-drive appointments (customer-facing; distinct from Phase 1 sales ``meetings``) --------------------


class Appointment(TenantMixin, _Stamped, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_appointments_client_id"),
        _tenant_fk("conversation_id", "conversations", "fk_appointments_conversation"),
        _tenant_fk("contact_id", "customer_contacts", "fk_appointments_contact"),
        _tenant_fk("vehicle_id", "vehicles", "fk_appointments_vehicle"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer)
    contact_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30), default="TEST_DRIVE", nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str | None] = mapped_column(String(40))
    customer_email: Mapped[str | None] = mapped_column(String(200))
    requested_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    confirmed_start: Mapped[datetime | None] = mapped_column(DateTime)
    # REQUESTED|PENDING_CONFIRMATION|CONFIRMED|DECLINED|CANCELLED|RESCHEDULE_REQUESTED|COMPLETED|NO_SHOW
    status: Mapped[str] = mapped_column(String(30), default="REQUESTED", nullable=False)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    calendar_provider: Mapped[str | None] = mapped_column(String(40))
    calendar_event_id: Mapped[str | None] = mapped_column(String(200))
    availability_checked: Mapped[str] = mapped_column(String(30), default="NOT_CHECKED", nullable=False)  # NOT_CHECKED|FREE|BUSY|NO_CALENDAR|CHECK_FAILED
    notes: Mapped[str | None] = mapped_column(Text)

    events: Mapped[list[AppointmentEvent]] = relationship(
        back_populates="appointment", order_by="AppointmentEvent.id", cascade="all, delete-orphan",
        foreign_keys="AppointmentEvent.appointment_id",
        primaryjoin="and_(Appointment.id == AppointmentEvent.appointment_id, "
                    "Appointment.client_id == AppointmentEvent.client_id)")


class AppointmentEvent(TenantMixin, Base):
    __tablename__ = "appointment_events"
    __table_args__ = (_tenant_fk("appointment_id", "appointments", "fk_appointment_events_appointment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    actor: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    appointment: Mapped[Appointment] = relationship(
        back_populates="events", foreign_keys=[appointment_id],
        primaryjoin="and_(Appointment.id == AppointmentEvent.appointment_id, "
                    "Appointment.client_id == AppointmentEvent.client_id)")


# --- Response automation (Modes A/B/C), approved content, authorizations, evaluations ---------------------


class ResponseAutomationSettings(TenantMixin, _Stamped, Base):
    __tablename__ = "response_automation_settings"
    __table_args__ = (UniqueConstraint("client_id", name="uq_response_automation_settings_client"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(30), default=MODE_DRAFT_ONLY, nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paused_reason: Mapped[str | None] = mapped_column(Text)
    paused_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime)
    max_auto_replies_per_conversation_per_hour: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_auto_replies_per_dealership_per_hour: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    min_seconds_between_auto_replies: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    auto_reply_outside_business_hours: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stale_inventory_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.85, nullable=False)
    # Mode C: extra intents the dealership explicitly approved for automation.
    controlled_intents: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    escalation_emails: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)


class EvaluationRun(Base):
    """A run of the synthetic EN/ES evaluation set. ``client_id`` is set when a dealership triggered it."""

    __tablename__ = "evaluation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    failures: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    triggered_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(Text)


class ModeAuthorization(TenantMixin, Base):
    """A dealership administrator's explicit authorization for an automation mode above draft-only."""

    __tablename__ = "mode_authorizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    authorized_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    evaluation_run_id: Mapped[int] = mapped_column(ForeignKey("evaluation_runs.id"), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class ResponseTemplate(TenantMixin, _Stamped, Base):
    """Dealer-approved reply template for an intent. Placeholders are filled from verified facts only."""

    __tablename__ = "response_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    intent: Mapped[str] = mapped_column(String(40), nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_send_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class DealerFAQ(TenantMixin, _Stamped, Base):
    __tablename__ = "dealer_faqs"

    id: Mapped[int] = mapped_column(primary_key=True)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_send_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- Staff notifications ------------------------------------------------------------------------------------


class NotificationPreference(TenantMixin, _Stamped, Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("client_id", name="uq_notification_preferences_client"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    immediate_alerts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    daily_digest: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    digest_hour_local: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    notify_assigned_user: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Escalation reasons that alert every dealership admin in addition to the assignee.
    escalate_to_admins_reasons: Mapped[list[Any]] = mapped_column(
        JSON, default=lambda: ["complaint", "human_request", "frustration", "sensitive_data"], nullable=False)
    # Dealership users who receive every escalation (Phase 3 ``memberships.receives_escalations``).
    escalation_user_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)


class Notification(TenantMixin, Base):
    """A staff notification with its real delivery status."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recipient_address: Mapped[str | None] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # IN_APP|EMAIL
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # ESCALATION|NEW_INQUIRY|DIGEST|APPOINTMENT|SYSTEM
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(String(400))
    # PENDING|SENDING|SENT (provider accepted)|DELIVERED (in-app stored)|FAILED|SIMULATED (NOT sent)|SKIPPED|BLOCKED
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- Shared send-safety infrastructure ----------------------------------------------------------------------


class RateCounter(Base):
    """Fixed-window counter shared by every web and worker process (multi-worker-safe rate limits)."""

    __tablename__ = "rate_counters"
    __table_args__ = (UniqueConstraint("key", "window_start", name="uq_rate_counter_window"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class SendAttempt(Base):
    """Outbound ledger row written (and committed) before a provider is called.

    IN_FLIGHT means "the provider may have accepted it": such a send is never retried automatically; an operator
    reconciles it (``UNCERTAIN``).
    """

    __tablename__ = "send_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)  # CUSTOMER_MESSAGE|NOTIFICATION_EMAIL
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient: Mapped[str] = mapped_column(String(200), nullable=False)
    live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # IN_FLIGHT|SENT|SIMULATED|FAILED|UNCERTAIN|BLOCKED
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

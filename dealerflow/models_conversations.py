"""Phase 3 models: customer conversations, response drafts, escalations, test-drive appointments, response
automation, notifications and the outbound send ledger.

Ported from the separate Phase 3 ``app`` package (branch ``claude/magical-darwin-nncp64``) onto the shared
``clients`` tenant. Conventions follow ``models_dealer``:

- Dealership-owned tables inherit ``TenantMixin`` (``client_id NOT NULL``); children reference parents
  through composite ``(client_id, parent_id)`` foreign keys, so the database refuses a row that points at
  another dealership's conversation, contact or vehicle.
- Platform-wide tables (send ledger, rate counters, pilot allowlist, raw webhook events, sandbox traffic,
  staff notifications, evaluation runs) carry a nullable ``client_id``.
- Status values keep Phase 3's upper-case vocabulary (documented on each column).

Customer contacts here are car buyers of a dealership. They are unrelated to ``prospects`` (dealerships we
prospect) and to the outreach ``suppressions`` list; customer opt-outs are per dealership and channel.
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
    Index,
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
    "CustomerContact", "CustomerIdentity", "CustomerLead", "ContactSuppression", "Conversation", "Message",
    "InboundEvent", "ResponseDraft", "Escalation", "Appointment", "AppointmentEvent", "ResponseAutomationSettings",
    "ModeAuthorization", "ApprovedTemplate", "FAQ", "EvaluationRun", "NotificationPreference", "Notification",
    "SendAttempt", "PilotRecipient", "RateCounter", "SandboxRecord",
]

MODE_DRAFT_ONLY = "A_DRAFT_ONLY"
MODE_FAQ = "B_FAQ_AUTOMATION"
MODE_CONTROLLED = "C_CONTROLLED"
MODES = (MODE_DRAFT_ONLY, MODE_FAQ, MODE_CONTROLLED)


class _Ts:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


def _tenant_fk(cols: list[str], table: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["client_id", *cols], [f"{table}.client_id", f"{table}.id"], name=name)


# --- customer CRM -----------------------------------------------------------------------------------


class CustomerContact(TenantMixin, _Ts, Base):
    __tablename__ = "customer_contacts"
    __table_args__ = (UniqueConstraint("client_id", "id", name="uq_customer_contacts_client_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    preferred_language: Mapped[str | None] = mapped_column(String(5))  # en | es
    # How contact details were collected (customer-provided in chat, ADF lead form, etc.)
    contact_source: Mapped[str | None] = mapped_column(String(100))
    sms_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sms_consent_source: Mapped[str | None] = mapped_column(Text)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)

    identities: Mapped[list[CustomerIdentity]] = relationship(
        viewonly=True,
        primaryjoin="and_(CustomerContact.id == foreign(CustomerIdentity.contact_id), "
                    "CustomerContact.client_id == CustomerIdentity.client_id)")


class CustomerIdentity(TenantMixin, Base):
    """Channel-specific identifier (Messenger PSID, phone number, chat visitor id...)."""

    __tablename__ = "customer_identities"
    __table_args__ = (
        UniqueConstraint("client_id", "channel", "external_id", name="uq_customer_identity"),
        _tenant_fk(["contact_id"], "customer_contacts", "fk_customer_identities_contact"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)

    contact: Mapped[CustomerContact] = relationship(
        viewonly=True,
        primaryjoin="and_(CustomerContact.id == foreign(CustomerIdentity.contact_id), "
                    "CustomerContact.client_id == CustomerIdentity.client_id)")


class CustomerLead(TenantMixin, _Ts, Base):
    """A car-buyer sales opportunity in the dealership's CRM, linked to a conversation when one exists."""

    __tablename__ = "customer_leads"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_customer_leads_client_id"),
        _tenant_fk(["contact_id"], "customer_contacts", "fk_customer_leads_contact"),
        _tenant_fk(["vehicle_id"], "vehicles", "fk_customer_leads_vehicle"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(40), nullable=False)  # channel or ADF provider
    status: Mapped[str] = mapped_column(String(30), default="NEW", nullable=False)  # NEW|CONTACTED|APPOINTMENT|SOLD|LOST
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    external_ref: Mapped[str | None] = mapped_column(String(200))


class ContactSuppression(TenantMixin, Base):
    """Per-dealership opt-out registry for customer messaging."""

    __tablename__ = "contact_suppressions"
    __table_args__ = (UniqueConstraint("client_id", "channel", "address", name="uq_contact_suppression"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    address: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), default="OPT_OUT", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --- unified inbox ----------------------------------------------------------------------------------


class Conversation(TenantMixin, _Ts, Base):
    """A car buyer's conversation with one dealership on one channel."""

    __tablename__ = "customer_conversations"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_customer_conversations_client_id"),
        UniqueConstraint("client_id", "channel", "external_conversation_id", name="uq_customer_conversation_external"),
        _tenant_fk(["contact_id"], "customer_contacts", "fk_customer_conversations_contact"),
        _tenant_fk(["vehicle_id"], "vehicles", "fk_customer_conversations_vehicle"),
        _tenant_fk(["lead_id"], "customer_leads", "fk_customer_conversations_lead"),
        Index("ix_customer_conversations_client_status", "client_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)  # WEBSITE_CHAT|META_MESSENGER|TWILIO_SMS|ADF_LEAD|MARKETPLACE_ASSISTED
    external_conversation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    lead_id: Mapped[int | None] = mapped_column(Integer)
    preferred_language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)  # OPEN|AWAITING_STAFF|AWAITING_CUSTOMER|ESCALATED|CLOSED
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
    # Reporting and retention fields used by Phase 4 (client reports, retention purge).
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # test/demo traffic, never reported as real
    lead_qualified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # a test drive was requested
    first_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime)  # first reply delivered (or recorded as sent manually)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime)
    summary_delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    pii_purged_at: Mapped[datetime | None] = mapped_column(DateTime)

    messages: Mapped[list[Message]] = relationship(
        viewonly=True, order_by="Message.id",
        primaryjoin="and_(Conversation.id == foreign(Message.conversation_id), Conversation.client_id == Message.client_id)")


class Message(TenantMixin, Base):
    __tablename__ = "customer_messages"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_customer_messages_client_id"),
        UniqueConstraint("channel", "provider_message_id", name="uq_customer_message_provider_id"),
        _tenant_fk(["conversation_id"], "customer_conversations", "fk_customer_messages_conversation"),
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
        viewonly=True,
        primaryjoin="and_(Conversation.id == foreign(Message.conversation_id), Conversation.client_id == Message.client_id)")


class InboundEvent(Base):
    """Raw provider event, stored once per provider event id (webhook / lead-feed dedupe)."""

    __tablename__ = "inbound_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_inbound_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="RECEIVED", nullable=False)  # RECEIVED|PROCESSED|IGNORED|FAILED
    error: Mapped[str | None] = mapped_column(Text)


class ResponseDraft(TenantMixin, _Ts, Base):
    __tablename__ = "response_drafts"
    __table_args__ = (
        _tenant_fk(["conversation_id"], "customer_conversations", "fk_response_drafts_conversation"),
        _tenant_fk(["in_reply_to_message_id"], "customer_messages", "fk_response_drafts_in_reply_to"),
        _tenant_fk(["sent_message_id"], "customer_messages", "fk_response_drafts_sent_message"),
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
    # Values of the verified facts at draft time; an AI send is blocked if any of them changed since.
    fact_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    unresolved: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    escalation_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(40))  # DRAFT_ONLY|AUTO_SEND|NO_REPLY
    decision_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    generated_by: Mapped[str] = mapped_column(String(40), default="template", nullable=False)
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_message_id: Mapped[int | None] = mapped_column(Integer)


class Escalation(TenantMixin, _Ts, Base):
    __tablename__ = "escalations"
    __table_args__ = (_tenant_fk(["conversation_id"], "customer_conversations", "fk_escalations_conversation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)  # OPEN|ACKNOWLEDGED|RESOLVED
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- test-drive appointments ------------------------------------------------------------------------


class Appointment(TenantMixin, _Ts, Base):
    """A customer's test-drive request. Never presented as confirmed until a staff member confirms it."""

    __tablename__ = "customer_appointments"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_customer_appointments_client_id"),
        _tenant_fk(["conversation_id"], "customer_conversations", "fk_customer_appointments_conversation"),
        _tenant_fk(["contact_id"], "customer_contacts", "fk_customer_appointments_contact"),
        _tenant_fk(["vehicle_id"], "vehicles", "fk_customer_appointments_vehicle"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True)
    contact_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30), default="TEST_DRIVE", nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str | None] = mapped_column(String(40))
    customer_email: Mapped[str | None] = mapped_column(String(200))
    requested_start: Mapped[datetime | None] = mapped_column(DateTime)
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
        viewonly=True, order_by="AppointmentEvent.id",
        primaryjoin="and_(Appointment.id == foreign(AppointmentEvent.appointment_id), "
                    "Appointment.client_id == AppointmentEvent.client_id)")


class AppointmentEvent(TenantMixin, Base):
    __tablename__ = "appointment_events"
    __table_args__ = (_tenant_fk(["appointment_id"], "customer_appointments", "fk_appointment_events_appointment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    actor: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    appointment: Mapped[Appointment] = relationship(
        viewonly=True,
        primaryjoin="and_(Appointment.id == foreign(AppointmentEvent.appointment_id), "
                    "Appointment.client_id == AppointmentEvent.client_id)")


# --- response automation ----------------------------------------------------------------------------


class ResponseAutomationSettings(TenantMixin, _Ts, Base):
    """Customer-response automation for one dealership (Phase 3 Modes A/B/C, limits, pause).

    Distinct from Phase 4's ``automation_settings`` (per-feature onboarding gates); auto-sending also
    requires Phase 4's ``inquiry_auto_response`` gate to be enabled.
    """

    __tablename__ = "response_automation_settings"
    __table_args__ = (UniqueConstraint("client_id", name="uq_response_automation_client"),)

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


class ModeAuthorization(TenantMixin, Base):
    """Explicit dealership authorization for an automation mode above draft-only."""

    __tablename__ = "mode_authorizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    authorized_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    evaluation_run_id: Mapped[int] = mapped_column(ForeignKey("evaluation_runs.id"), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class ApprovedTemplate(TenantMixin, _Ts, Base):
    """Dealer-approved response template for an intent. Placeholders are filled from verified facts only."""

    __tablename__ = "approved_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    intent: Mapped[str] = mapped_column(String(40), nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_send_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class FAQ(TenantMixin, _Ts, Base):
    __tablename__ = "faqs"

    id: Mapped[int] = mapped_column(primary_key=True)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_send_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class EvaluationRun(Base):
    """A run of the 46-case response-engine evaluation (required before Mode B/C can be authorized)."""

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


# --- staff notifications ----------------------------------------------------------------------------


class NotificationPreference(TenantMixin, _Ts, Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("client_id", name="uq_notification_preferences_client"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    immediate_alerts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    daily_digest: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    digest_hour_local: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    notify_assigned_user: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Escalation reasons that alert all dealership admins in addition to the assignee.
    escalate_to_admins_reasons: Mapped[list[Any]] = mapped_column(
        JSON, default=lambda: ["complaint", "human_request", "frustration", "sensitive_data"], nullable=False)


class Notification(Base):
    """In-app or email notification to a dealership user or (client_id NULL) to agency admins."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recipient_address: Mapped[str | None] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # IN_APP|EMAIL
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # ESCALATION|NEW_INQUIRY|DIGEST|APPOINTMENT|SYSTEM
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(String(400))
    # PENDING|SENDING|SENT (provider accepted)|DELIVERED (in-app stored)|FAILED|SIMULATED (sandbox, NOT sent)|SKIPPED|BLOCKED|UNCERTAIN
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- outbound safety --------------------------------------------------------------------------------


class SendAttempt(_Ts, Base):
    """One row per attempt to deliver a customer message or staff notification email (the outbound ledger).

    IN_FLIGHT is committed BEFORE the provider call. A row left IN_FLIGHT after a crash means the
    provider may or may not have accepted the message, so it is never retried automatically.
    """

    __tablename__ = "send_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # CUSTOMER_MESSAGE|NOTIFICATION_EMAIL
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"), index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient: Mapped[str] = mapped_column(String(200), nullable=False)
    live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # BLOCKED|IN_FLIGHT|SENT|SIMULATED|FAILED|UNCERTAIN
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(Text)


class PilotRecipient(Base):
    """Internal test recipients. In pilot mode, live customer sends to anyone else are blocked."""

    __tablename__ = "pilot_recipients"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_pilot_recipient"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # EMAIL|PHONE|META_PSID|CHAT_VISITOR
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class RateCounter(Base):
    """Fixed-window counters shared by every web and worker process (atomic conditional UPDATE)."""

    __tablename__ = "rate_counters"
    __table_args__ = (UniqueConstraint("key", "window_start", name="uq_rate_counter_window"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class SandboxRecord(Base):
    """Traffic a SIMULATED provider would have sent (customer messages, calendar events). Nothing left the system."""

    __tablename__ = "sandbox_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # CHANNEL_OUT|CAL_EVENT|EMAIL_OUT
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

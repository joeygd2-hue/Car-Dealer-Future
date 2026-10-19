"""Phase 3 models: car-buyer conversations with a client dealership, the response engine's drafts and
automation controls, test-drive appointments, staff notifications and the outbound send ledger.

Every dealership-owned table uses ``TenantMixin`` (``client_id NOT NULL``) so the tenant session filter and
flush guard in ``tenancy.py`` apply. Children point at their parent through composite
``(client_id, parent_id)`` foreign keys, as in ``models_dealer``, so a row can never reference another
dealership's conversation, contact, vehicle or appointment.

Phase 4's interim ``conversations`` / ``conversation_messages`` / ``appointments`` tables (``models_interim``)
are superseded by these; migration 0005 copied their rows here (``legacy_*_id``) and left them untouched.
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
    "MODE_DRAFT_ONLY", "MODE_FAQ", "MODE_CONTROLLED", "MODES", "CustomerContact", "CustomerIdentity", "CustomerLead",
    "ContactSuppression", "CustomerConversation", "CustomerMessage", "CustomerInboundEvent", "ResponseDraft",
    "Escalation", "TestDriveAppointment", "TestDriveAppointmentEvent", "ResponseAutomationSettings",
    "ModeAuthorization", "ApprovedResponseTemplate", "FAQ", "EvaluationRun", "DealerNotificationPreference",
    "DealerNotification", "SendAttempt", "RateCounter",
]

MODE_DRAFT_ONLY = "A_DRAFT_ONLY"
MODE_FAQ = "B_FAQ_AUTOMATION"
MODE_CONTROLLED = "C_CONTROLLED"
MODES = (MODE_DRAFT_ONLY, MODE_FAQ, MODE_CONTROLLED)


class _Ts:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


def _fk(cols: list[str], target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["client_id", *cols], [f"{target}.client_id", f"{target}.id"], name=name)


# --- customers (end buyers of a client dealership; not outreach prospects) ---------------------------


class CustomerContact(TenantMixin, _Ts, Base):
    __tablename__ = "customer_contacts"
    __table_args__ = (UniqueConstraint("client_id", "id", name="uq_customer_contacts_client_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    preferred_language: Mapped[str | None] = mapped_column(String(5))  # en | es
    contact_source: Mapped[str | None] = mapped_column(String(100))  # how the details were collected
    sms_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sms_consent_source: Mapped[str | None] = mapped_column(Text)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)

    identities: Mapped[list[CustomerIdentity]] = relationship(back_populates="contact", cascade="all, delete-orphan")


class CustomerIdentity(TenantMixin, Base):
    """Channel-specific identifier (Messenger PSID, phone number, chat visitor id …)."""

    __tablename__ = "customer_identities"
    __table_args__ = (
        UniqueConstraint("client_id", "channel", "external_id", name="uq_customer_identity"),
        _fk(["contact_id"], "customer_contacts", "fk_customer_identities_contact"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)

    contact: Mapped[CustomerContact] = relationship(back_populates="identities", overlaps="identities")


class CustomerLead(TenantMixin, _Ts, Base):
    """Sales opportunity in the dealership's CRM, linked to a conversation when one exists."""

    __tablename__ = "customer_leads"
    __table_args__ = (
        _fk(["contact_id"], "customer_contacts", "fk_customer_leads_contact"),
        _fk(["vehicle_id"], "vehicles", "fk_customer_leads_vehicle"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(40), nullable=False)  # channel or ADF provider
    status: Mapped[str] = mapped_column(String(30), default="NEW", nullable=False)  # NEW|CONTACTED|APPOINTMENT|SOLD|LOST
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    external_ref: Mapped[str | None] = mapped_column(String(200))


class ContactSuppression(TenantMixin, Base):
    """Per-dealership opt-out registry for customer messaging (outreach suppressions are ``suppressions``)."""

    __tablename__ = "contact_suppressions"
    __table_args__ = (UniqueConstraint("client_id", "channel", "address", name="uq_contact_suppression"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    address: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), default="OPT_OUT", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --- unified inbox ----------------------------------------------------------------------------------------


class CustomerConversation(TenantMixin, _Ts, Base):
    __tablename__ = "customer_conversations"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_customer_conversations_client_id"),
        UniqueConstraint("client_id", "channel", "external_conversation_id", name="uq_customer_conversation_external"),
        _fk(["contact_id"], "customer_contacts", "fk_customer_conversations_contact"),
        _fk(["vehicle_id"], "vehicles", "fk_customer_conversations_vehicle"),
        Index("ix_customer_conversations_client_status", "client_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # WEBSITE_CHAT | META_MESSENGER | TWILIO_SMS | ADF_LEAD | MARKETPLACE_ASSISTED
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    external_conversation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("customer_leads.id", ondelete="SET NULL"))
    preferred_language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    # OPEN | AWAITING_STAFF | AWAITING_CUSTOMER | ESCALATED | CLOSED
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)
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
    # Reporting (Phase 4 client reports): first customer message, first reply that left (or was recorded as sent
    # manually), first escalation, and whether the customer gave contact details or asked for a test drive.
    first_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime)
    lead_qualified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # True for test/demo conversations (sandbox inquiries, evaluation); excluded from client reports.
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pii_purged_at: Mapped[datetime | None] = mapped_column(DateTime)
    legacy_conversation_id: Mapped[int | None] = mapped_column(Integer)  # row copied from interim ``conversations``

    messages: Mapped[list[CustomerMessage]] = relationship(
        back_populates="conversation", order_by="CustomerMessage.id", cascade="all, delete-orphan", overlaps="messages"
    )


class CustomerMessage(TenantMixin, Base):
    __tablename__ = "customer_messages"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_customer_messages_client_id"),
        UniqueConstraint("channel", "provider_message_id", name="uq_customer_message_provider_id"),
        _fk(["conversation_id"], "customer_conversations", "fk_customer_messages_conversation"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # INBOUND | OUTBOUND | NOTE
    sender_type: Mapped[str] = mapped_column(String(20), nullable=False)  # CUSTOMER | STAFF | AI_AUTO | SYSTEM
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
    legacy_message_id: Mapped[int | None] = mapped_column(Integer)

    conversation: Mapped[CustomerConversation] = relationship(back_populates="messages", overlaps="messages")


class CustomerInboundEvent(Base):
    """Raw provider event, stored once per provider event id (webhook dedupe). ``client_id`` is NULL until the
    event is matched to a dealership (e.g. an unknown Messenger page)."""

    __tablename__ = "customer_inbound_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_customer_inbound_event"),)

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
    __table_args__ = (_fk(["conversation_id"], "customer_conversations", "fk_response_drafts_conversation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    in_reply_to_message_id: Mapped[int | None] = mapped_column(ForeignKey("customer_messages.id", ondelete="SET NULL"))
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
    decision: Mapped[str | None] = mapped_column(String(40))  # DRAFT_ONLY | AUTO_SEND
    decision_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    generated_by: Mapped[str] = mapped_column(String(40), default="template", nullable=False)
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_message_id: Mapped[int | None] = mapped_column(ForeignKey("customer_messages.id", ondelete="SET NULL"))


class Escalation(TenantMixin, _Ts, Base):
    __tablename__ = "escalations"
    __table_args__ = (_fk(["conversation_id"], "customer_conversations", "fk_escalations_conversation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)  # OPEN|ACKNOWLEDGED|RESOLVED
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- test-drive appointments (customer side; outreach sales meetings are ``meetings``) ----------------------


class TestDriveAppointment(TenantMixin, _Ts, Base):
    __tablename__ = "test_drive_appointments"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_test_drive_appointments_client_id"),
        _fk(["conversation_id"], "customer_conversations", "fk_test_drive_appointments_conversation"),
        _fk(["contact_id"], "customer_contacts", "fk_test_drive_appointments_contact"),
        _fk(["vehicle_id"], "vehicles", "fk_test_drive_appointments_vehicle"),
    )
    __test__ = False  # not a pytest test class

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True)
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
    # NOT_CHECKED | FREE | BUSY | NO_CALENDAR | CHECK_FAILED
    availability_checked: Mapped[str] = mapped_column(String(30), default="NOT_CHECKED", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    legacy_appointment_id: Mapped[int | None] = mapped_column(Integer)

    events: Mapped[list[TestDriveAppointmentEvent]] = relationship(
        back_populates="appointment", order_by="TestDriveAppointmentEvent.id", cascade="all, delete-orphan", overlaps="events"
    )


class TestDriveAppointmentEvent(TenantMixin, Base):
    __tablename__ = "test_drive_appointment_events"
    __table_args__ = (_fk(["appointment_id"], "test_drive_appointments", "fk_test_drive_appointment_events_appointment"),)
    __test__ = False

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    actor: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    appointment: Mapped[TestDriveAppointment] = relationship(back_populates="events", overlaps="events")


# --- automation configuration, approved content, authorizations, evaluations --------------------------------


class ResponseAutomationSettings(TenantMixin, _Ts, Base):
    """Response-engine mode and limits for one dealership. (Phase 4's ``automation_settings`` are per-feature
    on/off gates; Mode B/C auto-sending needs both.)"""

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
    # Unguessable key for the public website-chat widget (/chat/{key}). Rotatable by a manager.
    chat_widget_key: Mapped[str | None] = mapped_column(String(64), unique=True)


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
    revoked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class ApprovedResponseTemplate(TenantMixin, _Ts, Base):
    """Dealer-approved response template for an intent. Placeholders are filled from verified facts only.
    (Outreach email templates are ``templates``.)"""

    __tablename__ = "approved_response_templates"

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
    """One run of the synthetic evaluation set (engine-wide, so not tenant-owned; ``client_id`` records who ran it)."""

    __tablename__ = "evaluation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    failures: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    triggered_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(Text)


# --- staff notifications --------------------------------------------------------------------------------------


class DealerNotificationPreference(TenantMixin, _Ts, Base):
    __tablename__ = "dealer_notification_preferences"
    __table_args__ = (UniqueConstraint("client_id", name="uq_dealer_notification_preferences_client"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    immediate_alerts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    daily_digest: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    digest_hour_local: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    notify_assigned_user: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Escalation reasons that alert all dealership admins in addition to the assignee.
    escalate_to_admins_reasons: Mapped[list[Any]] = mapped_column(
        JSON, default=lambda: ["complaint", "human_request", "frustration", "sensitive_data"], nullable=False
    )


class DealerNotification(TenantMixin, Base):
    """A notification to a dealership user about a customer conversation, with its real delivery status."""

    __tablename__ = "dealer_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recipient_address: Mapped[str | None] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # IN_APP | EMAIL
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # ESCALATION|NEW_INQUIRY|DIGEST|APPOINTMENT|SYSTEM
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(String(400))
    # PENDING|SENDING|SENT (provider accepted)|DELIVERED (in-app stored)|FAILED|SIMULATED (NOT sent)|NOT_SENT|SKIPPED|UNCERTAIN
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- outbound ledger and distributed counters ----------------------------------------------------------------


class SendAttempt(Base):
    """One attempt to deliver a message to someone outside the platform. Committed IN_FLIGHT *before* the
    provider call; an attempt still IN_FLIGHT after a crash becomes UNCERTAIN and is never retried automatically."""

    __tablename__ = "send_attempts"
    __table_args__ = (Index("ix_send_attempts_kind_ref", "kind", "ref_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # CUSTOMER_MESSAGE | DEALER_NOTIFICATION_EMAIL | …
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"), index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient: Mapped[str] = mapped_column(String(200), nullable=False)
    live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # BLOCKED|IN_FLIGHT|SENT|SIMULATED|FAILED|UNCERTAIN
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class RateCounter(Base):
    """Fixed-window counter shared by every web and worker process (see services/customer/rate_limits.py)."""

    __tablename__ = "rate_counters"
    __table_args__ = (UniqueConstraint("key", "window_start", name="uq_rate_counter_window"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

"""Phase 3: dealership customer conversations (ported from the Phase 3 ``app/`` package).

Customers here are a client dealership's car buyers, never the agency's outreach prospects (Phase 1).

Conventions (see models_dealer.py): every table is tenant-owned (``TenantMixin``), tenant parents
declare ``UniqueConstraint("client_id", "id")`` and children reference them with composite
``(client_id, parent_id)`` foreign keys, so the database refuses cross-dealership links.
Statuses and channel codes are lowercase.

Channels: ``website_chat`` (first-party widget), ``adf_lead`` (ADF/XML lead intake), ``marketplace_assisted``
(staff paste Facebook Marketplace messages; replies are sent by staff in Facebook). Only website chat can be
delivered by the platform, and only when live customer messaging is authorized (see services/conversations).

Migration 0005 converted the interim Phase 4 ``conversations``/``conversation_messages``/``appointments``
tables into these, keeping their rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow
from .models_dealer import TenantMixin

__all__ = [
    "CHANNELS", "SENDABLE_CHANNELS", "MODES", "MODE_DRAFT_ONLY", "MODE_FAQ", "MODE_CONTROLLED",
    "CustomerContact", "CustomerIdentity", "CustomerLead", "CustomerSuppression", "Conversation", "ConversationMessage",
    "ResponseDraft", "ConversationEscalation", "Appointment", "AppointmentEvent", "ResponseAutomationSettings",
    "ResponseModeAuthorization", "ResponseTemplate", "DealerFAQ", "ResponseEvaluationRun", "StaffNotification",
    "RateCounter",
]

WEBSITE_CHAT, ADF_LEAD, MARKETPLACE_ASSISTED = "website_chat", "adf_lead", "marketplace_assisted"
CHANNELS = (WEBSITE_CHAT, ADF_LEAD, MARKETPLACE_ASSISTED)
SENDABLE_CHANNELS = (WEBSITE_CHAT,)  # the others are answered by staff outside the platform

MODE_DRAFT_ONLY, MODE_FAQ, MODE_CONTROLLED = "a_draft_only", "b_faq_automation", "c_controlled"
MODES = (MODE_DRAFT_ONLY, MODE_FAQ, MODE_CONTROLLED)


class CustomerContact(TenantMixin, Base):
    __tablename__ = "customer_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    preferred_language: Mapped[str | None] = mapped_column(String(5))
    contact_source: Mapped[str | None] = mapped_column(String(100))  # how the details were collected
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pii_purged_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("client_id", "id", name="uq_customer_contacts_client_id"),
                      Index("ix_customer_contacts_client_email", "client_id", "email"))


class CustomerIdentity(TenantMixin, Base):
    """Channel-specific identifier for a contact (chat visitor id, ADF lead email, Marketplace thread)."""

    __tablename__ = "customer_identities"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "channel", "external_id", name="uq_customer_identities_external"),
        ForeignKeyConstraint(["client_id", "contact_id"], ["customer_contacts.client_id", "customer_contacts.id"],
                             name="fk_customer_identities_contact", ondelete="CASCADE"),
    )


class CustomerLead(TenantMixin, Base):
    """A sales opportunity for the dealership, created with the conversation that started it."""

    __tablename__ = "customer_leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(40), nullable=False)  # channel
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False)  # new|contacted|appointment|sold|lost
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    external_ref: Mapped[str | None] = mapped_column(String(200))
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_customer_leads_client_id"),
        ForeignKeyConstraint(["client_id", "contact_id"], ["customer_contacts.client_id", "customer_contacts.id"],
                             name="fk_customer_leads_contact"),
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"], name="fk_customer_leads_vehicle"),
    )


class CustomerSuppression(TenantMixin, Base):
    """Per-dealership opt-out list for customer messaging (distinct from the agency's prospect ``suppressions``)."""

    __tablename__ = "customer_suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    address: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), default="opt_out", nullable=False)
    source: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("client_id", "channel", "address", name="uq_customer_suppressions"),)


class Conversation(TenantMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    external_conversation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    lead_id: Mapped[int | None] = mapped_column(Integer)
    preferred_language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    # open|awaiting_staff|awaiting_customer|escalated|closed
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    human_takeover: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    takeover_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    takeover_at: Mapped[datetime | None] = mapped_column(DateTime)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)
    human_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lead_qualified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    summary_delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    unresolved_questions: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    next_action: Mapped[str | None] = mapped_column(Text)
    appointment_status: Mapped[str] = mapped_column(String(30), default="none", nullable=False)
    escalation_status: Mapped[str] = mapped_column(String(20), default="none", nullable=False)  # none|open|resolved
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    pii_purged_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_conversations_client_id"),
        UniqueConstraint("client_id", "channel", "external_conversation_id", name="uq_conversations_external"),
        ForeignKeyConstraint(["client_id", "contact_id"], ["customer_contacts.client_id", "customer_contacts.id"],
                             name="fk_conversations_contact"),
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"], name="fk_conversations_vehicle"),
        ForeignKeyConstraint(["client_id", "lead_id"], ["customer_leads.client_id", "customer_leads.id"], name="fk_conversations_lead"),
        Index("ix_conversations_client_status", "client_id", "status"),
    )


class ConversationMessage(TenantMixin, Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # inbound|outbound|note
    sender_type: Mapped[str] = mapped_column(String(20), nullable=False)  # customer|staff|ai_auto|system
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(5))
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    # received | queued | sending | sent | simulated | manual_external | blocked | failed
    # | uncertain (a send may or may not have reached the customer: never re-sent automatically)
    delivery_status: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    delivery_error: Mapped[str | None] = mapped_column(Text)
    sending_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_conversation_messages_client_id"),
        UniqueConstraint("client_id", "channel", "provider_message_id", name="uq_conversation_messages_provider"),
        ForeignKeyConstraint(["client_id", "conversation_id"], ["conversations.client_id", "conversations.id"],
                             name="fk_conversation_messages_conversation", ondelete="CASCADE"),
    )


class ResponseDraft(TenantMixin, Base):
    """A reply composed from verified facts. Never sent without the configured authorization."""

    __tablename__ = "response_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    in_reply_to_message_id: Mapped[int | None] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    # pending_review|sent|auto_sent|rejected|superseded|blocked|send_failed
    status: Mapped[str] = mapped_column(String(20), default="pending_review", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    intents: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    facts_used: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    # Values of the verified facts at draft time; an automatic send is blocked if any of them changed since.
    fact_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    unresolved: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    escalation_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(20))  # draft_only|auto_send
    decision_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    generated_by: Mapped[str] = mapped_column(String(40), default="template", nullable=False)  # template|template+llm|legacy
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_response_drafts_client_id"),
        ForeignKeyConstraint(["client_id", "conversation_id"], ["conversations.client_id", "conversations.id"],
                             name="fk_response_drafts_conversation", ondelete="CASCADE"),
    )


class ConversationEscalation(TenantMixin, Base):
    __tablename__ = "conversation_escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)  # open|resolved
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["client_id", "conversation_id"], ["conversations.client_id", "conversations.id"],
                             name="fk_conversation_escalations_conversation", ondelete="CASCADE"),
    )


APPOINTMENT_STATUSES = ("requested", "pending_confirmation", "confirmed", "declined", "cancelled",
                        "reschedule_requested", "completed", "no_show")


class Appointment(TenantMixin, Base):
    """A customer's test-drive request. Never presented to the customer as confirmed until staff confirm it."""

    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer)
    contact_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="test_drive", nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str | None] = mapped_column(String(40))
    customer_email: Mapped[str | None] = mapped_column(String(200))
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    requested_start: Mapped[datetime | None] = mapped_column(DateTime)  # NULL: customer has not proposed a time
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    confirmed_start: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), default="requested", nullable=False)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    availability_checked: Mapped[str] = mapped_column(String(20), default="not_checked", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_appointments_client_id"),
        CheckConstraint("status in ('requested','pending_confirmation','confirmed','declined','cancelled',"
                        "'reschedule_requested','completed','no_show')", name="ck_appointments_status_v2"),
        ForeignKeyConstraint(["client_id", "conversation_id"], ["conversations.client_id", "conversations.id"],
                             name="fk_appointments_conversation"),
        ForeignKeyConstraint(["client_id", "contact_id"], ["customer_contacts.client_id", "customer_contacts.id"],
                             name="fk_appointments_contact"),
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"], name="fk_appointments_vehicle"),
    )


class AppointmentEvent(TenantMixin, Base):
    __tablename__ = "appointment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["client_id", "appointment_id"], ["appointments.client_id", "appointments.id"],
                             name="fk_appointment_events_appointment", ondelete="CASCADE"),
    )


class ResponseAutomationSettings(TenantMixin, Base):
    """Per-dealership response mode and limits. Draft-only (mode A) is the default."""

    __tablename__ = "response_automation_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), default=MODE_DRAFT_ONLY, nullable=False)
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
    controlled_intents: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)  # mode C extras
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("client_id", name="uq_response_automation_settings_client"),)


class ResponseEvaluationRun(Base):
    """A run of the synthetic evaluation set (isolated in-memory database; no tenant data)."""

    __tablename__ = "response_evaluation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    failures: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    triggered_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ResponseModeAuthorization(TenantMixin, Base):
    """The dealership's written authorization for a mode above draft-only, tied to a passing evaluation."""

    __tablename__ = "response_mode_authorizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    authorized_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    evaluation_run_id: Mapped[int] = mapped_column(ForeignKey("response_evaluation_runs.id"), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class ResponseTemplate(TenantMixin, Base):
    """Dealer-approved reply wording for an intent. Placeholders are filled from verified facts only."""

    __tablename__ = "response_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    intent: Mapped[str] = mapped_column(String(40), nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_send_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class DealerFAQ(TenantMixin, Base):
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class StaffNotification(TenantMixin, Base):
    """Alert for dealership staff, with the real delivery status (in_app rows are simply stored)."""

    __tablename__ = "staff_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)  # in_app|email
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # escalation|new_inquiry|appointment|system
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(20), default="stored", nullable=False)  # stored|sent|simulated|not_sent|failed
    status_detail: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class RateCounter(Base):
    """Fixed-window counter shared by every web and worker process (see services/rate_limits.py)."""

    __tablename__ = "rate_counters"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("key", "window_start", name="uq_rate_counters_window"),)

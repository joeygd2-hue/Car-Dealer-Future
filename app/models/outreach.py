"""Persistent autonomous outreach agent (prospecting independent dealerships).

These models are the shared Phase 1 CRM/outreach foundation. Phase 1 work should extend them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow
from app.models.base import IdMixin, TimestampMixin


class OutreachAgentState(IdMixin, Base):
    """Singleton row describing the outreach agent's state machine and operating limits."""

    __tablename__ = "outreach_agent_state"

    # STOPPED|RUNNING|PAUSED|EMERGENCY_STOPPED
    state: Mapped[str] = mapped_column(String(30), default="STOPPED")
    state_reason: Mapped[str | None] = mapped_column(Text)
    changed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago")
    send_window_start_hour: Mapped[int] = mapped_column(Integer, default=9)
    send_window_end_hour: Mapped[int] = mapped_column(Integer, default=17)
    send_weekdays: Mapped[list] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4])
    max_sends_per_day: Mapped[int] = mapped_column(Integer, default=25)
    max_sends_per_hour: Mapped[int] = mapped_column(Integer, default=8)
    daily_budget_cents: Mapped[int] = mapped_column(Integer, default=500)
    target_queue_size: Mapped[int] = mapped_column(Integer, default=20)
    max_discoveries_per_tick: Mapped[int] = mapped_column(Integer, default=10)
    max_task_retries: Mapped[int] = mapped_column(Integer, default=4)
    target_cities: Mapped[list] = mapped_column(
        JSON,
        default=lambda: ["Sugar Land", "Stafford", "Missouri City", "Richmond", "Rosenberg", "Houston", "Katy", "Pearland"],
    )
    auto_respond_routine: Mapped[bool] = mapped_column(Boolean, default=False)
    # Incremented on every start so task idempotency keys from a stopped run never block a new run.
    run_generation: Mapped[int] = mapped_column(Integer, default=0)
    inbox_cursor: Mapped[dict] = mapped_column(JSON, default=dict)


class OutreachFAQ(IdMixin, TimestampMixin, Base):
    """Approved company information the agent may use to answer routine prospect questions."""

    __tablename__ = "outreach_faqs"

    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    language: Mapped[str] = mapped_column(String(5), default="en")
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Prospect(IdMixin, TimestampMixin, Base):
    __tablename__ = "prospects"
    __table_args__ = (UniqueConstraint("source", "source_ref"),)

    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)
    address: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(40))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(200), index=True)
    contact_name: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40))  # CSV|GOOGLE_PLACES|SANDBOX|MANUAL
    source_ref: Mapped[str | None] = mapped_column(String(200))  # e.g. Google place_id
    source_url: Mapped[str | None] = mapped_column(String(400))
    # Places-derived content must be refreshed/purged (see docs/research).
    source_content_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # NEW|RESEARCHED|QUALIFIED|NEEDS_REVIEW|DISQUALIFIED|ENROLLED|REPLIED|MEETING_BOOKED|CONVERTED|DO_NOT_CONTACT
    status: Mapped[str] = mapped_column(String(30), default="NEW", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    fit_score: Mapped[int] = mapped_column(Integer, default=0)
    email_verified_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime)
    converted_dealership_id: Mapped[int | None] = mapped_column(ForeignKey("dealerships.id"))

    facts: Mapped[list[ProspectFact]] = relationship(back_populates="prospect", cascade="all, delete-orphan")


class ProspectFact(IdMixin, Base):
    """Evidence-backed fact/observation. Drafts may only reference facts recorded here."""

    __tablename__ = "prospect_facts"

    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    value: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(String(400))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    confidence: Mapped[str] = mapped_column(String(20), default="SOURCE_REPORTED")  # VERIFIED|SOURCE_REPORTED|INFERRED
    is_observation: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    prospect: Mapped[Prospect] = relationship(back_populates="facts")


class Campaign(IdMixin, TimestampMixin, Base):
    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT|ACTIVE|PAUSED|COMPLETED
    channel: Mapped[str] = mapped_column(String(20), default="EMAIL")
    # [{"day_offset": 0, "subject": "...", "body": "..."}] — templates with {placeholders}
    steps: Mapped[list] = mapped_column(JSON, default=list)
    daily_send_limit: Mapped[int] = mapped_column(Integer, default=20)
    daily_budget_cents: Mapped[int] = mapped_column(Integer, default=300)
    # When False, drafts built only from VERIFIED facts may send without per-message approval.
    require_message_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    content_approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    content_approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class Enrollment(IdMixin, TimestampMixin, Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("campaign_id", "prospect_id"),)

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE|STOPPED|COMPLETED
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(60))

    campaign: Mapped[Campaign] = relationship()
    prospect: Mapped[Prospect] = relationship()


class OutreachMessage(IdMixin, TimestampMixin, Base):
    __tablename__ = "outreach_messages"

    enrollment_id: Mapped[int | None] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(20), default="SEQUENCE")  # SEQUENCE|REPLY
    channel: Mapped[str] = mapped_column(String(20), default="EMAIL")
    to_address: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    # DRAFT|PENDING_APPROVAL|APPROVED|SENDING|SENT|SIMULATED|FAILED|BLOCKED|CANCELLED
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    approval_reasons: Mapped[list] = mapped_column(JSON, default=list)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(300), index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    in_reply_to: Mapped[str | None] = mapped_column(String(300))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    block_reasons: Mapped[list] = mapped_column(JSON, default=list)
    facts_used: Mapped[list] = mapped_column(JSON, default=list)
    unsubscribe_token: Mapped[str | None] = mapped_column(String(100), unique=True)

    prospect: Mapped[Prospect] = relationship()


class OutreachReply(IdMixin, Base):
    __tablename__ = "outreach_replies"
    __table_args__ = (UniqueConstraint("provider_message_id"),)

    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    outreach_message_id: Mapped[int | None] = mapped_column(ForeignKey("outreach_messages.id", ondelete="SET NULL"))
    provider_message_id: Mapped[str] = mapped_column(String(300))
    from_address: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # POSITIVE|MEETING_REQUEST|QUESTION|NOT_INTERESTED|OPT_OUT|OUT_OF_OFFICE|BOUNCE|SENSITIVE|UNKNOWN
    classification: Mapped[str | None] = mapped_column(String(30))
    classification_confidence: Mapped[float | None] = mapped_column(Float)
    classification_signals: Mapped[list] = mapped_column(JSON, default=list)
    requires_human: Mapped[bool] = mapped_column(Boolean, default=False)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    action_taken: Mapped[str | None] = mapped_column(Text)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)

    prospect: Mapped[Prospect | None] = relationship()


class Suppression(IdMixin, Base):
    """Global do-not-contact list for outreach (email, domain, phone)."""

    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("kind", "value"),)

    kind: Mapped[str] = mapped_column(String(10))  # EMAIL|DOMAIN|PHONE
    value: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(30))  # OPT_OUT|BOUNCE|MANUAL_DNC|COMPLAINT|EXISTING_CLIENT
    source: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SalesMeeting(IdMixin, TimestampMixin, Base):
    """Operator's sales meetings with prospects. Distinct from dealership customer Appointments."""

    __tablename__ = "sales_meetings"

    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    booking_token: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="PROPOSED")  # PROPOSED|CONFIRMED|CANCELLED|COMPLETED|FAILED
    start_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    calendar_event_id: Mapped[str | None] = mapped_column(String(300))
    video_provider: Mapped[str | None] = mapped_column(String(30))
    video_meeting_id: Mapped[str | None] = mapped_column(String(200))
    video_join_url: Mapped[str | None] = mapped_column(String(500))
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)

    prospect: Mapped[Prospect] = relationship()


class CostEntry(IdMixin, Base):
    __tablename__ = "cost_entries"

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    scope: Mapped[str] = mapped_column(String(30))  # OUTREACH|CUSTOMER_RESPONSE|PLATFORM
    dealership_id: Mapped[int | None] = mapped_column(ForeignKey("dealerships.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    provider: Mapped[str] = mapped_column(String(40))
    operation: Mapped[str] = mapped_column(String(60))
    units: Mapped[float] = mapped_column(Float, default=1.0)
    amount_cents: Mapped[float] = mapped_column(Float, default=0.0)
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=True)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    reference: Mapped[str | None] = mapped_column(String(200))

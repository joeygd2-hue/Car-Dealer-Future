"""Phase 1 models: prospects, the autonomous outreach agent, replies, meetings."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import IdMixin, TimestampMixin, utcnow

PROSPECT_STAGES = (
    "new",
    "researching",
    "qualified",
    "disqualified",
    "contacted",
    "replied",
    "interested",
    "meeting_booked",
    "not_interested",
    "opted_out",
    "converted",
)
# Stages in which no further outreach may be sent.
STOP_STAGES = ("disqualified", "replied", "interested", "meeting_booked", "not_interested", "opted_out", "converted")


class Prospect(IdMixin, TimestampMixin, Base):
    __tablename__ = "prospects"
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    address_line1: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(50))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    phone: Mapped[str | None] = mapped_column(String(40))
    website: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(254))
    email_source_url: Mapped[str | None] = mapped_column(String(500))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(200))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(20), default="new", nullable=False, index=True)
    priority: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_independent: Mapped[bool | None] = mapped_column(Boolean)
    qualification_notes: Mapped[str | None] = mapped_column(Text)
    disqualify_reason: Mapped[str | None] = mapped_column(String(200))
    # Evidence-backed facts: [{"fact": str, "source_url": str, "observed_at": iso, "method": "measured"}]
    evidence: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime)
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime)
    research_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        CheckConstraint("stage in (" + ",".join(f"'{s}'" for s in PROSPECT_STAGES) + ")", name="ck_prospect_stage"),
        Index("ix_prospect_stage_priority", "stage", "priority"),
    )


class Suppression(IdMixin, Base):
    """Do-not-contact list. Checked before every outreach action."""

    __tablename__ = "suppressions"
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # email | domain | phone
    value: Mapped[str] = mapped_column(String(254), nullable=False)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    __table_args__ = (
        UniqueConstraint("kind", "value", name="uq_suppression"),
        CheckConstraint("kind in ('email','domain','phone')", name="ck_suppression_kind"),
        CheckConstraint(
            "reason in ('opt_out','bounce','complaint','manual_dnc','legal','existing_client')",
            name="ck_suppression_reason",
        ),
    )


class AgentConfig(IdMixin, TimestampMixin, Base):
    """Singleton configuration and state of the autonomous outreach agent."""

    __tablename__ = "agent_config"
    # running | paused | stopped | emergency_stopped
    state: Mapped[str] = mapped_column(String(20), default="stopped", nullable=False)
    state_reason: Mapped[str | None] = mapped_column(Text)
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    state_changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Live operation must be explicitly authorized by an admin. Until then only sandbox providers are used.
    live_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    live_authorized_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    live_authorized_at: Mapped[datetime | None] = mapped_column(DateTime)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago", nullable=False)
    send_days: Mapped[list] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4], nullable=False)  # Mon=0
    send_start_hour: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    send_end_hour: Mapped[int] = mapped_column(Integer, default=17, nullable=False)
    daily_send_limit: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    hourly_send_limit: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    per_domain_daily_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    daily_budget_micros: Mapped[int] = mapped_column(Integer, default=5_000_000, nullable=False)  # $5.00
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_followups: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    followup_days: Mapped[list] = mapped_column(JSON, default=lambda: [4, 9], nullable=False)
    min_qualified_queue: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    research_batch_size: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    tick_action_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    # Channels the agent may use. Only "email" is implemented; others require new authorization.
    enabled_channels: Mapped[list] = mapped_column(JSON, default=lambda: ["email"], nullable=False)
    discovery_queries: Mapped[list] = mapped_column(
        JSON, default=lambda: ["used car dealer Sugar Land TX", "used car dealer Stafford TX"], nullable=False
    )
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_tick_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Campaign(IdMixin, TimestampMixin, Base):
    __tablename__ = "campaigns"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)  # draft|active|paused|archived
    channel: Mapped[str] = mapped_column(String(20), default="email", nullable=False)
    # Ordered steps: [{"template_id": int, "delay_days": int}]
    steps: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # When True, messages rendered purely from an approved template may be sent without per-message approval.
    auto_send_approved_templates: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_send_authorized_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    daily_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    target_cities: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    __table_args__ = (
        CheckConstraint("status in ('draft','active','paused','archived')", name="ck_campaign_status"),
        CheckConstraint("channel in ('email')", name="ck_campaign_channel"),
    )


class MessageTemplate(IdMixin, TimestampMixin, Base):
    """Human-approved message template. Placeholders refer only to verified prospect fields."""

    __tablename__ = "message_templates"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class CompanyFact(IdMixin, TimestampMixin, Base):
    """Approved company information the agent may use to answer routine questions."""

    __tablename__ = "company_facts"
    topic: Mapped[str] = mapped_column(String(60), nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    answer_en: Mapped[str] = mapped_column(Text, nullable=False)
    answer_es: Mapped[str | None] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_reply_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Enrollment(IdMixin, TimestampMixin, Base):
    """A prospect's position in a campaign sequence."""

    __tablename__ = "enrollments"
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True, nullable=False)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active|stopped|completed
    next_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(60))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)

    prospect: Mapped[Prospect] = relationship()
    campaign: Mapped[Campaign] = relationship()
    __table_args__ = (UniqueConstraint("prospect_id", "campaign_id", name="uq_enrollment"),)


class OutreachMessage(IdMixin, TimestampMixin, Base):
    __tablename__ = "outreach_messages"
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True, nullable=False)
    enrollment_id: Mapped[int | None] = mapped_column(ForeignKey("enrollments.id", ondelete="SET NULL"), index=True)
    step_index: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="initial", nullable=False)  # initial|followup|reply
    channel: Mapped[str] = mapped_column(String(20), default="email", nullable=False)
    to_address: Mapped[str | None] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("message_templates.id", ondelete="SET NULL"))
    # template | ai | human
    generated_by: Mapped[str] = mapped_column(String(10), default="template", nullable=False)
    # draft | pending_approval | approved | queued | sent | blocked | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)
    requires_approval_reason: Mapped[str | None] = mapped_column(String(200))
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    blocked_reason: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    provider: Mapped[str | None] = mapped_column(String(40))
    provider_message_id: Mapped[str | None] = mapped_column(String(300), index=True)
    reply_token: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Plain integer (no FK) to avoid a circular FK with inbound_messages.
    in_reply_to_inbound_id: Mapped[int | None] = mapped_column(Integer)

    prospect: Mapped[Prospect] = relationship()


class InboundMessage(IdMixin, Base):
    """A reply or other inbound email from a prospect (or unmatched)."""

    __tablename__ = "inbound_messages"
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(300))
    from_address: Mapped[str] = mapped_column(String(254), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"), index=True)
    outreach_message_id: Mapped[int | None] = mapped_column(ForeignKey("outreach_messages.id", ondelete="SET NULL"))
    match_method: Mapped[str | None] = mapped_column(String(30))
    # positive|meeting_request|question|not_interested|opt_out|out_of_office|bounce|sensitive|unknown
    classification: Mapped[str | None] = mapped_column(String(20))
    classification_method: Mapped[str | None] = mapped_column(String(10))  # rule | ai
    classification_confidence: Mapped[float | None] = mapped_column(Float)
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_action: Mapped[str | None] = mapped_column(String(60))


class Meeting(IdMixin, TimestampMixin, Base):
    __tablename__ = "meetings"
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"), index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(300), unique=True)
    video_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="booked", nullable=False)  # booked|cancelled|held|no_show
    booked_by: Mapped[str] = mapped_column(String(20), default="agent", nullable=False)  # agent | human


class AgentActivity(IdMixin, Base):
    """Auditable log of every agent action, outcome, error, cost and state transition."""

    __tablename__ = "agent_activity"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)  # ok | skipped | blocked | error
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    job_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_basis: Mapped[str | None] = mapped_column(String(20))
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str | None] = mapped_column(String(30))

"""Database models.

Conventions:
- Timestamps are naive UTC (see ``db.utcnow``).
- Facts about a dealership that came from outside are recorded as ``Evidence`` rows with
  the source URL and collection time, so every displayed claim can be traced.
- Anything produced by a mock provider carries ``simulated=True``.
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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# "client" is a dealership-client user bound to one ``clients`` row. It is deliberately the lowest
# rank, so require("viewer") and above never admit it; web.deps.current_user additionally confines
# client users to the client portal.
ROLES = ("client", "viewer", "rep", "manager", "admin")
STAFF_ROLES = ("viewer", "rep", "manager", "admin")

DEALERSHIP_TYPES = ("independent_used", "buy_here_pay_here", "franchise", "wholesale", "other", "unknown")

# Default pipeline. Stored in ``pipeline_stages`` so labels/order can be changed; keys are stable.
DEFAULT_STAGES: list[tuple[str, str, bool]] = [
    ("new", "New", False),
    ("researching", "Researching", False),
    ("qualified", "Qualified", False),
    ("ready_for_outreach", "Ready for outreach", False),
    ("outreach_approved", "Outreach approved", False),
    ("contacted", "Contacted", False),
    ("replied", "Replied", False),
    ("interested", "Interested", False),
    ("meeting_requested", "Meeting requested", False),
    ("meeting_booked", "Meeting booked", False),
    ("proposal_sent", "Proposal sent", False),
    ("negotiation", "Negotiation", False),
    ("won", "Won", True),
    ("lost", "Lost", True),
    ("follow_up_later", "Follow up later", False),
    ("do_not_contact", "Do not contact", True),
]

MESSAGE_STATES = ("draft", "approved", "queued", "sending", "sent", "delivered", "bounced", "replied", "failed", "cancelled")
MEETING_STATES = ("requested", "pending_approval", "booked", "rescheduled", "cancelled", "completed", "no_show", "failed")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(20), default="rep", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_hash: Mapped[str | None] = mapped_column(String(128), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Set only for role == "client": the dealership client this user belongs to.
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE", name="fk_users_client_id", use_alter=True), index=True)

    @property
    def is_client(self) -> bool:
        return self.role == "client"


class PipelineStage(Base):
    __tablename__ = "pipeline_stages"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Prospect(TimestampMixin, Base):
    """A dealership (business). Contact people are in ``Contact``."""

    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    dealership_type: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    website: Mapped[str | None] = mapped_column(String(500))
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    inventory_url: Mapped[str | None] = mapped_column(String(500))
    address_line: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120), index=True)
    state: Mapped[str | None] = mapped_column(String(2))
    postal_code: Mapped[str | None] = mapped_column(String(10), index=True)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    phone: Mapped[str | None] = mapped_column(String(40))
    phone_e164: Mapped[str | None] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    preferred_language: Mapped[str] = mapped_column(String(5), default="en")
    stage: Mapped[str] = mapped_column(String(40), ForeignKey("pipeline_stages.key"), default="new", index=True)
    lead_source: Mapped[str] = mapped_column(String(60), default="manual", index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    research_notes: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float | None] = mapped_column(Float, index=True)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id"))

    contacts: Mapped[list[Contact]] = relationship(back_populates="prospect", cascade="all, delete-orphan")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="prospect", cascade="all, delete-orphan")
    sources: Mapped[list[ProspectSource]] = relationship(back_populates="prospect", cascade="all, delete-orphan")
    activities: Mapped[list[Activity]] = relationship(
        back_populates="prospect", cascade="all, delete-orphan", order_by="Activity.created_at.desc()"
    )
    owner: Mapped[User | None] = relationship()


class Contact(TimestampMixin, Base):
    """A person at a dealership. Only stored when the details come from a cited public source."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    prospect: Mapped[Prospect] = relationship(back_populates="contacts")


class Evidence(Base):
    """One sourced fact about a prospect (field value, qualification signal or research note)."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(60), nullable=False)  # e.g. phone, website, signal:spanish_content
    value: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)  # google_places, osm, csv, manual, website
    source_url: Mapped[str | None] = mapped_column(String(1000))
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(Text, default="")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)

    prospect: Mapped[Prospect] = relationship(back_populates="evidence")


class ProspectSource(Base):
    """Link between a prospect and a record in an external source (used for dedup)."""

    __tablename__ = "prospect_sources"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_source_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    prospect: Mapped[Prospect] = relationship(back_populates="sources")


class Activity(Base):
    """Timeline entry: research, email_out, email_in, call, meeting, proposal, note, task, stage, score, system."""

    __tablename__ = "activities"
    __table_args__ = (Index("ix_activity_prospect_time", "prospect_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    ref_type: Mapped[str | None] = mapped_column(String(40))
    ref_id: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    prospect: Mapped[Prospect] = relationship(back_populates="activities")
    actor: Mapped[User | None] = relationship()


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), default="follow_up")  # follow_up, call, meeting_prep, review
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open, done, cancelled
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str] = mapped_column(Text, default="")
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True)

    prospect: Mapped[Prospect | None] = relationship()


class Template(TimestampMixin, Base):
    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("name", "language", name="uq_template_name_lang"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    language: Mapped[str] = mapped_column(String(5), default="en")
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class OutreachMessage(TimestampMixin, Base):
    __tablename__ = "outreach_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id", ondelete="SET NULL"))
    to_email: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(5), default="en")
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime)
    provider: Mapped[str | None] = mapped_column(String(30))
    provider_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    enrollment_step: Mapped[int | None] = mapped_column(Integer)
    approval_kind: Mapped[str | None] = mapped_column(String(20))  # human | campaign_auto
    origin: Mapped[str] = mapped_column(String(20), default="human")  # human | agent
    in_reply_to_inbound_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL", use_alter=True, name="fk_outreach_reply_inbound"))
    review_flags: Mapped[list[Any] | None] = mapped_column(JSON)  # reasons a person must review

    prospect: Mapped[Prospect] = relationship()


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    __table_args__ = (UniqueConstraint("provider", "provider_message_id", name="uq_inbound_provider_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rfc_message_id: Mapped[str | None] = mapped_column(String(255))
    in_reply_to: Mapped[str | None] = mapped_column(String(1000))
    thread_id: Mapped[str | None] = mapped_column(String(255))
    from_email: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"), index=True)
    outreach_message_id: Mapped[int | None] = mapped_column(ForeignKey("outreach_messages.id", ondelete="SET NULL"))
    matched_by: Mapped[str | None] = mapped_column(String(30))  # thread, in_reply_to, contact_email, domain, manual
    classification: Mapped[str] = mapped_column(String(30), default="reply")  # reply, opt_out, meeting_request, bounce, auto_reply
    classification_confidence: Mapped[float | None] = mapped_column(Float)
    classification_reasons: Mapped[list[Any] | None] = mapped_column(JSON)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    prospect: Mapped[Prospect | None] = relationship()


class Suppression(Base):
    """Addresses or domains that must never receive outreach. Checked before every send."""

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)  # lowercased email or @domain
    kind: Mapped[str] = mapped_column(String(10), default="email")  # email | domain | phone
    reason: Mapped[str] = mapped_column(String(60), nullable=False)  # unsubscribe, reply_opt_out, bounce, manual
    source: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Meeting(TimestampMixin, Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="requested", index=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime)
    attendee_email: Mapped[str | None] = mapped_column(String(255))
    topic: Mapped[str] = mapped_column(String(255), default="")
    proposed_slots: Mapped[list[Any] | None] = mapped_column(JSON)
    availability_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    availability_source: Mapped[str | None] = mapped_column(String(30))
    requested_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    zoom_meeting_id: Mapped[str | None] = mapped_column(String(64))
    zoom_join_url: Mapped[str | None] = mapped_column(String(1000))
    calendar_event_id: Mapped[str | None] = mapped_column(String(255))
    provider_response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    prep_notes: Mapped[str] = mapped_column(Text, default="")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)

    prospect: Mapped[Prospect] = relationship()


class Proposal(TimestampMixin, Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    monthly_amount: Mapped[float | None] = mapped_column(Numeric(10, 2))
    setup_amount: Mapped[float | None] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft, sent, accepted, rejected
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str] = mapped_column(Text, default="")

    prospect: Mapped[Prospect] = relationship()


class Call(TimestampMixin, Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    to_number: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), default="manual")
    provider_call_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="initiated")  # initiated, completed, failed, logged
    outcome: Mapped[str] = mapped_column(String(40), default="")  # connected, voicemail, no_answer, wrong_number, opt_out
    notes: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)


class CostEntry(Base):
    """Money spent on acquisition (data APIs, tools, ads). Used for CAC when present."""

    __tablename__ = "cost_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    incurred_on: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    lead_source: Mapped[str | None] = mapped_column(String(60))
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    note: Mapped[str] = mapped_column(String(500), default="")
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ref: Mapped[str | None] = mapped_column(String(120))  # e.g. job key or provider request id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Phase 4 usage tracking. ``quantity``/``metric`` are what the provider reported (measured);
    # ``verified`` means ``amount`` was copied from provider billing data rather than estimated.
    provider: Mapped[str | None] = mapped_column(String(80), index=True)
    metric: Mapped[str | None] = mapped_column(String(60))
    quantity: Mapped[float | None] = mapped_column(Numeric(18, 4))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL", name="fk_cost_entries_client_id"), index=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)

    @property
    def basis(self) -> str:
        if self.verified:
            return "verified_billing"
        if self.quantity is not None and not float(self.amount or 0) and not self.is_estimate:
            return "unknown"  # usage measured, but no rate on file: cost is not claimed
        return "estimated" if self.is_estimate else "recorded"


class AppSetting(Base):
    """Editable non-secret configuration (scoring weights, etc.)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    actor: Mapped[User | None] = relationship()


class IntegrationLog(Base):
    """Every call to an external provider (real or simulated), with outcome."""

    __tablename__ = "integration_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # ok, error, auth_error, rate_limited
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Job(Base):
    """Background job. ``key`` makes enqueueing idempotent."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending, running, done, failed, dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- Autonomous outreach agent -------------------------------------------------------------

AGENT_STATES = ("stopped", "running", "paused", "emergency_stopped")


class AgentState(Base):
    """Singleton row (id=1) holding the agent's control switches, checkpoint and heartbeat."""

    __tablename__ = "agent_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="stopped", nullable=False)
    status_reason: Mapped[str] = mapped_column(String(500), default="")
    status_changed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Live operation (real sends through real providers) needs an explicit human authorization.
    live_authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    live_authorized_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    live_authorized_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    worker_id: Mapped[str | None] = mapped_column(String(120))
    last_tick_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_tick_finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # per-step cursors / last-run times
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # limits, schedule, budgets (see services.agent)


class AgentRun(Base):
    """One bounded agent tick: what it did, counts and errors."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running, ok, partial, skipped, error
    skipped_reason: Mapped[str | None] = mapped_column(String(255))
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    errors: Mapped[list[Any] | None] = mapped_column(JSON)


class Campaign(TimestampMixin, Base):
    """An outreach sequence. ``steps`` = [{"template": name, "delay_days": n}, ...] (step 0 delay ignored)."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="paused")  # active, paused, archived
    channel: Mapped[str] = mapped_column(String(20), default="email")
    language_mode: Mapped[str] = mapped_column(String(10), default="auto")  # auto (prospect pref), en, es
    steps: Mapped[list[Any]] = mapped_column(JSON, default=list)
    min_score: Mapped[float] = mapped_column(Float, default=55)
    daily_cap: Mapped[int] = mapped_column(Integer, default=10)
    target_cities: Mapped[str] = mapped_column(String(500), default="")  # empty = use global target
    # Approval mode: "manual" = every message waits for a person; "auto" = messages rendered purely
    # from an approved template may be sent by the agent once live operation is authorized.
    approval_mode: Mapped[str] = mapped_column(String(10), default="manual")
    auto_authorized_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    auto_authorized_at: Mapped[datetime | None] = mapped_column(DateTime)
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class Enrollment(TimestampMixin, Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("campaign_id", "prospect_id", name="uq_enrollment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)  # active, stopped, completed
    step: Mapped[int] = mapped_column(Integer, default=0)  # index of the next step to send
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(120))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)

    campaign: Mapped[Campaign] = relationship()
    prospect: Mapped[Prospect] = relationship()


class KnowledgeItem(TimestampMixin, Base):
    """Approved company information the agent may use to answer routine questions."""

    __tablename__ = "knowledge_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(120), nullable=False)
    keywords: Mapped[str] = mapped_column(String(500), nullable=False)  # comma-separated, any language
    answer_en: Mapped[str] = mapped_column(Text, default="")
    answer_es: Mapped[str] = mapped_column(Text, default="")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


# Phase 2-4 models live in their own module; importing it here registers them with Alembic.
from .models_ops import *  # noqa: E402,F401,F403

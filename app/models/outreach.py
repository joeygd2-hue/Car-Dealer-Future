"""Outreach agent and CRM models (agency-level; prospects are not dealership-owned data).

Phase 1 CRM code did not exist in the repository, so these tables are the
shared CRM: a Prospect is a dealership we may sell to. When one signs, a
``clients`` row is created and linked through ``Prospect.converted_client_id``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import new_id, utcnow
from app.db import Base

PROSPECT_STATUSES = (
    "new", "researching", "qualified", "disqualified", "enrolled", "contacted", "replied",
    "meeting_booked", "won", "lost", "do_not_contact",
)


class Prospect(Base):
    __tablename__ = "prospects"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pro"))
    business_name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(200), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(40))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(40))  # google_places | csv_import | manual | sandbox
    source_ref: Mapped[str | None] = mapped_column(String(200))  # e.g. Google place_id
    source_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Dedupe key: domain if known, else normalized name + postal code.
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="new")
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    qualification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    disqualify_reason: Mapped[str | None] = mapped_column(String(300))
    # Simulated prospects can only ever be contacted through sandbox providers.
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    converted_client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"))
    last_enriched_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (Index("ix_prospects_status", "status", "priority_score"),)


class ProspectContact(Base):
    __tablename__ = "prospect_contacts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pct"))
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(254), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    # Where we found this contact (URL + excerpt). Contacts without evidence are not used.
    evidence_url: Mapped[str | None] = mapped_column(Text)
    evidence_excerpt: Mapped[str | None] = mapped_column(Text)
    # B2B email basis under CAN-SPAM: business address published by the business.
    email_basis: Mapped[str | None] = mapped_column(String(60))
    # SMS/voice require documented prior express consent; None = no consent.
    sms_consent_at: Mapped[datetime | None] = mapped_column()
    call_consent_at: Mapped[datetime | None] = mapped_column()
    email_status: Mapped[str] = mapped_column(String(20), default="unverified")  # unverified | ok | bounced | invalid
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProspectObservation(Base):
    """An evidence-backed fact about a prospect. Drafts may only cite these."""
    __tablename__ = "prospect_observations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("obs"))
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    statement: Mapped[str] = mapped_column(Text)
    evidence_url: Mapped[str | None] = mapped_column(Text)
    evidence_excerpt: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40))
    observed_at: Mapped[datetime] = mapped_column(default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cmp"))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | active | paused | completed
    channel: Mapped[str] = mapped_column(String(20), default="email")
    # [{"step": 1, "delay_days": 0, "goal": "intro"}, {"step": 2, "delay_days": 3, "goal": "follow_up"}, ...]
    sequence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    target_criteria: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # {"timezone": "America/Chicago", "days": [0..4], "start_hour": 9, "end_hour": 17}
    operating_schedule: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    daily_send_limit: Mapped[int] = mapped_column(Integer, default=20)
    hourly_send_limit: Mapped[int] = mapped_column(Integer, default=5)
    daily_budget_usd: Mapped[float] = mapped_column(Float, default=2.0)
    max_followups: Mapped[int] = mapped_column(Integer, default=2)
    # Human approval gates. First touches default to approval-required.
    require_approval_first_touch: Mapped[bool] = mapped_column(Boolean, default=True)
    require_approval_followups: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_reply_routine: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_book_meetings: Mapped[bool] = mapped_column(Boolean, default=False)
    email_integration_id: Mapped[str | None] = mapped_column(ForeignKey("platform_integrations.id"))
    calendar_integration_id: Mapped[str | None] = mapped_column(ForeignKey("platform_integrations.id"))
    sender_name: Mapped[str] = mapped_column(String(120), default="")
    sender_email: Mapped[str] = mapped_column(String(254), default="")
    # CAN-SPAM: valid physical postal address in every commercial email.
    postal_address: Mapped[str] = mapped_column(String(300), default="")
    live_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    live_authorized_by: Mapped[str | None] = mapped_column(String(40))
    live_authorized_at: Mapped[datetime | None] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Enrollment(Base):
    __tablename__ = "campaign_enrollments"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("enr"))
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id"), index=True)
    contact_id: Mapped[str] = mapped_column(ForeignKey("prospect_contacts.id"))
    # needs_draft | awaiting_approval | scheduled | waiting | replied | meeting_booked | completed | stopped
    state: Mapped[str] = mapped_column(String(30), default="needs_draft")
    current_step: Mapped[int] = mapped_column(Integer, default=0)  # last step sent
    next_action_at: Mapped[datetime | None] = mapped_column()
    stop_reason: Mapped[str | None] = mapped_column(String(200))
    thread_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("campaign_id", "prospect_id", name="uq_enrollment"),)


class OutreachMessage(Base):
    __tablename__ = "outreach_messages"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("msg"))
    enrollment_id: Mapped[str | None] = mapped_column(ForeignKey("campaign_enrollments.id"), index=True)
    # Nullable only for inbound mail we could not match to a prospect (escalated).
    prospect_id: Mapped[str | None] = mapped_column(ForeignKey("prospects.id"), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("prospect_contacts.id"))
    integration_id: Mapped[str | None] = mapped_column(ForeignKey("platform_integrations.id"))
    direction: Mapped[str] = mapped_column(String(10))  # outbound | inbound
    channel: Mapped[str] = mapped_column(String(20), default="email")
    step: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30), default="sequence")  # sequence | reply | meeting_offer | meeting_confirmation
    subject: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    from_address: Mapped[str | None] = mapped_column(String(254))
    to_address: Mapped[str | None] = mapped_column(String(254))
    # outbound: draft | awaiting_approval | approved | rejected | sending | sent | failed | blocked
    # inbound: received | processed
    status: Mapped[str] = mapped_column(String(20))
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    approved_by: Mapped[str | None] = mapped_column(String(40))
    approved_at: Mapped[datetime | None] = mapped_column()
    provider: Mapped[str | None] = mapped_column(String(40))
    provider_message_id: Mapped[str | None] = mapped_column(String(300), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(300), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    send_after: Mapped[datetime | None] = mapped_column()
    sent_at: Mapped[datetime | None] = mapped_column()
    received_at: Mapped[datetime | None] = mapped_column()
    classification: Mapped[str | None] = mapped_column(String(40))
    classification_confidence: Mapped[float | None] = mapped_column(Float)
    classification_method: Mapped[str | None] = mapped_column(String(20))  # rules | ai
    policy_checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    generator: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Suppression(Base):
    """Do-not-contact list. Checked before every outreach action."""
    __tablename__ = "suppressions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sup"))
    kind: Mapped[str] = mapped_column(String(10))  # email | domain | phone
    value: Mapped[str] = mapped_column(String(254))
    reason: Mapped[str] = mapped_column(String(30))  # opt_out | bounce | complaint | manual_dnc | legal | not_interested
    source: Mapped[str | None] = mapped_column(String(120))
    expires_at: Mapped[datetime | None] = mapped_column()  # None = permanent
    created_by: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_suppression"),)


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("mtg"))
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id"), index=True)
    enrollment_id: Mapped[str | None] = mapped_column(ForeignKey("campaign_enrollments.id"))
    status: Mapped[str] = mapped_column(String(20))  # offered | booked | cancelled | completed
    offered_slots: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    start_at: Mapped[datetime | None] = mapped_column()
    end_at: Mapped[datetime | None] = mapped_column()
    provider: Mapped[str | None] = mapped_column(String(40))
    calendar_event_id: Mapped[str | None] = mapped_column(String(300))
    video_link: Mapped[str | None] = mapped_column(Text)
    attendee_email: Mapped[str | None] = mapped_column(String(254))
    booked_by: Mapped[str | None] = mapped_column(String(40))  # 'agent' or user id
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Escalation(Base):
    """Something the agent must not decide alone; a human resolves it."""
    __tablename__ = "escalations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("esc"))
    prospect_id: Mapped[str | None] = mapped_column(ForeignKey("prospects.id"), index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("outreach_messages.id"))
    kind: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved
    resolved_by: Mapped[str | None] = mapped_column(String(40))
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column()


class CompanyFact(Base):
    """Approved company information the agent may use to answer routine questions."""
    __tablename__ = "company_facts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cfa"))
    topic: Mapped[str] = mapped_column(String(80), unique=True)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    approved_by: Mapped[str | None] = mapped_column(String(40))
    approved_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CostEntry(Base):
    """Operating cost ledger. ``measured`` distinguishes provider-reported usage from estimates."""
    __tablename__ = "cost_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(30))  # ai | email | places | calendar
    provider: Mapped[str] = mapped_column(String(40))
    units: Mapped[float] = mapped_column(Float, default=0)
    unit_label: Mapped[str] = mapped_column(String(30), default="")
    amount_usd: Mapped[float] = mapped_column(Float, default=0)
    measured: Mapped[bool] = mapped_column(Boolean, default=False)
    campaign_id: Mapped[str | None] = mapped_column(String(40))
    ref: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class AgentRun(Base):
    """One bounded agent tick: explicit state + checkpoint so restarts resume cleanly."""
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("arn"))
    state: Mapped[str] = mapped_column(String(30))  # running | completed | skipped_paused | stopped | error
    phase: Mapped[str | None] = mapped_column(String(30))  # last phase reached (checkpoint)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column()


class SandboxMail(Base):
    """Sandbox mailbox: simulated outbound deliveries and injectable inbound replies."""
    __tablename__ = "sandbox_mail"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sbm"))
    direction: Mapped[str] = mapped_column(String(10))  # outbound | inbound
    from_address: Mapped[str] = mapped_column(String(254))
    to_address: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str] = mapped_column(String(200), index=True)
    message_id: Mapped[str] = mapped_column(String(200), unique=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(200))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

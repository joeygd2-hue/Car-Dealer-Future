"""Phase 3: unified inbox — conversations, messages, provider events, drafts, escalations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow
from app.models.base import IdMixin, TimestampMixin


class Conversation(IdMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("dealership_id", "channel", "external_conversation_id"),)

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(40))  # WEBSITE_CHAT|META_MESSENGER|TWILIO_SMS|ADF_LEAD|MARKETPLACE_ASSISTED
    external_conversation_id: Mapped[str] = mapped_column(String(200))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"))
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("customer_leads.id", ondelete="SET NULL"))
    preferred_language: Mapped[str] = mapped_column(String(5), default="en")
    status: Mapped[str] = mapped_column(String(30), default="OPEN")  # OPEN|AWAITING_STAFF|AWAITING_CUSTOMER|ESCALATED|CLOSED
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    human_takeover: Mapped[bool] = mapped_column(Boolean, default=False)
    takeover_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    takeover_at: Mapped[datetime | None] = mapped_column(DateTime)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)
    human_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    unresolved_questions: Mapped[list] = mapped_column(JSON, default=list)
    next_action: Mapped[str | None] = mapped_column(Text)
    appointment_status: Mapped[str] = mapped_column(String(30), default="NONE")
    escalation_status: Mapped[str] = mapped_column(String(20), default="NONE")  # NONE|OPEN|RESOLVED
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.id", cascade="all, delete-orphan"
    )


class Message(IdMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("channel", "provider_message_id"),)

    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(10))  # INBOUND|OUTBOUND|NOTE
    sender_type: Mapped[str] = mapped_column(String(20))  # CUSTOMER|STAFF|AI_AUTO|SYSTEM
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(5))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # RECEIVED|QUEUED|SENDING|SENT|FAILED|SIMULATED|MANUAL_EXTERNAL|BLOCKED
    delivery_status: Mapped[str] = mapped_column(String(20), default="RECEIVED")
    delivery_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class InboundEvent(IdMixin, Base):
    """Raw provider event, stored once per provider event id (webhook dedupe)."""

    __tablename__ = "inbound_events"
    __table_args__ = (UniqueConstraint("provider", "event_id"),)

    provider: Mapped[str] = mapped_column(String(40))
    event_id: Mapped[str] = mapped_column(String(200))
    dealership_id: Mapped[int | None] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="RECEIVED")  # RECEIVED|PROCESSED|IGNORED|FAILED
    error: Mapped[str | None] = mapped_column(Text)


class ResponseDraft(IdMixin, TimestampMixin, Base):
    __tablename__ = "response_drafts"

    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    in_reply_to_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(5))
    # PENDING_REVIEW|SENT|AUTO_SENT|REJECTED|SUPERSEDED|BLOCKED|SEND_FAILED
    status: Mapped[str] = mapped_column(String(20), default="PENDING_REVIEW")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    intents: Mapped[list] = mapped_column(JSON, default=list)
    facts_used: Mapped[list] = mapped_column(JSON, default=list)
    unresolved: Mapped[list] = mapped_column(JSON, default=list)
    escalation_reasons: Mapped[list] = mapped_column(JSON, default=list)
    decision: Mapped[str | None] = mapped_column(String(40))  # DRAFT_ONLY|AUTO_SEND|NO_REPLY
    decision_reasons: Mapped[list] = mapped_column(JSON, default=list)
    generated_by: Mapped[str] = mapped_column(String(40), default="template")
    engine_version: Mapped[str] = mapped_column(String(20))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))


class Escalation(IdMixin, TimestampMixin, Base):
    __tablename__ = "escalations"

    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text)
    recommended_action: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN|ACKNOWLEDGED|RESOLVED
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

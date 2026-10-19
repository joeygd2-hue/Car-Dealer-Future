"""INTERIM Phase 3 tables (customer conversations, appointments), used by Phase 4 reporting and
end-to-end tests until the Phase 3 customer-response engine is ported into dealerflow.

The interim inventory/listing/publishing tables that used to live here were replaced by the real
Phase 2 models (``models_dealer.py``) when the branches were integrated. Conversations reference
Phase 2 ``vehicles``. See docs/architecture/shared-stack.md ("Phase 4 interim tables").
"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow
from .models_ops import _Ts

__all__ = ["Conversation", "ConversationMessage", "Appointment"]


class Conversation(_Ts, Base):
    """A car buyer's inquiry to a client dealership on a connected channel."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_contact: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)  # open|escalated|closed
    lead_qualified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime)
    summary: Mapped[str | None] = mapped_column(Text)
    summary_delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    pii_purged_at: Mapped[datetime | None] = mapped_column(DateTime)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # inbound | outbound
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    provider_message_id: Mapped[str | None] = mapped_column(String(300), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class Appointment(_Ts, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("status in ('requested','confirmed','cancelled','completed','no_show')", name="ck_appointments_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20), default="test_drive", nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="requested", nullable=False)

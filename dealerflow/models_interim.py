"""DEPRECATED (superseded by ``models_customer``; kept so no data is dropped without the owner's decision).

INTERIM Phase 3 tables (customer conversations, appointments), used by Phase 4 reporting and the
end-to-end test until the real Phase 3 models land in dealerflow. Inventory, listings and publishing
are Phase 2's (models_dealer.py).

Kept in their own module and their own migration (0004) so the Phase 3 port can replace them cleanly:
add a migration that drops or converts these tables, then point ``services/client_reports.py`` and
``services/dealer_ops.py`` at the real models. See docs/architecture/shared-stack.md.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow
from .models_ops import _Ts

__all__ = ["InterimAppointment", "InterimConversation", "InterimConversationMessage"]



class InterimConversation(_Ts, Base):
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


class InterimConversationMessage(Base):
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


class InterimAppointment(_Ts, Base):
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

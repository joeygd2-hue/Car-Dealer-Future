"""Operational safety tables: send-attempt ledger, pilot allowlist, distributed rate counters, password resets."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.models.base import IdMixin, TimestampMixin


class SendAttempt(IdMixin, TimestampMixin, Base):
    """One row per attempt to deliver anything outside the platform (the outbound ledger).

    IN_FLIGHT is committed BEFORE the provider call. A row left IN_FLIGHT after a crash means the
    provider may or may not have accepted the message, so it is never retried automatically.
    """

    __tablename__ = "send_attempts"

    kind: Mapped[str] = mapped_column(String(30), index=True)  # CUSTOMER_MESSAGE|OUTREACH_EMAIL|NOTIFICATION_EMAIL
    ref_id: Mapped[int] = mapped_column(Integer, index=True)
    dealership_id: Mapped[int | None] = mapped_column(ForeignKey("dealerships.id", ondelete="SET NULL"), index=True)
    channel: Mapped[str] = mapped_column(String(40))
    recipient: Mapped[str] = mapped_column(String(200))
    live: Mapped[bool] = mapped_column(Boolean, default=False)
    # BLOCKED|IN_FLIGHT|SENT|SIMULATED|FAILED|UNCERTAIN
    status: Mapped[str] = mapped_column(String(20), index=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(Text)


class PilotRecipient(IdMixin, Base):
    """Internal test recipients. In pilot mode, live sends to anyone else are blocked."""

    __tablename__ = "pilot_recipients"
    __table_args__ = (UniqueConstraint("kind", "value"),)

    kind: Mapped[str] = mapped_column(String(20))  # EMAIL|PHONE|META_PSID|CHAT_VISITOR
    value: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(200))
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RateCounter(IdMixin, Base):
    """Fixed-window counters shared by every web and worker process (atomic conditional UPDATE)."""

    __tablename__ = "rate_counters"
    __table_args__ = (UniqueConstraint("key", "window_start"),)

    key: Mapped[str] = mapped_column(String(200))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    count: Mapped[int] = mapped_column(Integer, default=0)


class PasswordResetToken(IdMixin, Base):
    __tablename__ = "password_reset_tokens"

    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(20), default="RESET")  # RESET|SETUP
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)

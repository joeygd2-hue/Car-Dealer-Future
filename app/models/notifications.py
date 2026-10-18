"""Staff notifications with real delivery status."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.models.base import IdMixin, TimestampMixin


class NotificationPreference(IdMixin, TimestampMixin, Base):
    __tablename__ = "notification_preferences"

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), unique=True)
    immediate_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_digest: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_hour_local: Mapped[int] = mapped_column(Integer, default=8)
    notify_assigned_user: Mapped[bool] = mapped_column(Boolean, default=True)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Escalation reasons that alert all dealership admins in addition to the assignee.
    escalate_to_admins_reasons: Mapped[list] = mapped_column(
        JSON, default=lambda: ["complaint", "human_request", "frustration", "sensitive_data"]
    )


class Notification(IdMixin, Base):
    __tablename__ = "notifications"

    dealership_id: Mapped[int | None] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recipient_address: Mapped[str | None] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20))  # IN_APP|EMAIL
    kind: Mapped[str] = mapped_column(String(30))  # ESCALATION|NEW_INQUIRY|DIGEST|APPOINTMENT|SYSTEM
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(400))
    # PENDING|SENT (provider accepted)|DELIVERED (in-app stored)|FAILED|SIMULATED (sandbox, NOT sent)|SKIPPED
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    status_detail: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)

"""Phase 3: automation configuration, approved content, authorizations and evaluations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.models.base import IdMixin, TimestampMixin

MODE_DRAFT_ONLY = "A_DRAFT_ONLY"
MODE_FAQ = "B_FAQ_AUTOMATION"
MODE_CONTROLLED = "C_CONTROLLED"
MODES = (MODE_DRAFT_ONLY, MODE_FAQ, MODE_CONTROLLED)


class AutomationSettings(IdMixin, TimestampMixin, Base):
    __tablename__ = "automation_settings"

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), unique=True)
    mode: Mapped[str] = mapped_column(String(30), default=MODE_DRAFT_ONLY)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_reason: Mapped[str | None] = mapped_column(Text)
    paused_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime)
    max_auto_replies_per_conversation_per_hour: Mapped[int] = mapped_column(Integer, default=3)
    max_auto_replies_per_dealership_per_hour: Mapped[int] = mapped_column(Integer, default=30)
    min_seconds_between_auto_replies: Mapped[int] = mapped_column(Integer, default=30)
    auto_reply_outside_business_hours: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_inventory_hours: Mapped[int] = mapped_column(Integer, default=24)
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.85)
    # Mode C: extra intents the dealership explicitly approved for automation.
    controlled_intents: Mapped[list] = mapped_column(JSON, default=list)
    escalation_emails: Mapped[list] = mapped_column(JSON, default=list)


class ModeAuthorization(IdMixin, Base):
    """Explicit dealership authorization for an automation mode above draft-only."""

    __tablename__ = "mode_authorizations"

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(30))
    statement: Mapped[str] = mapped_column(Text)
    authorized_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    authorized_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    evaluation_run_id: Mapped[int] = mapped_column(ForeignKey("evaluation_runs.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class ApprovedTemplate(IdMixin, TimestampMixin, Base):
    """Dealer-approved response template for an intent. Placeholders are filled from verified facts only."""

    __tablename__ = "approved_templates"

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    intent: Mapped[str] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(5))
    body: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_send_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class FAQ(IdMixin, TimestampMixin, Base):
    __tablename__ = "faqs"

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    language: Mapped[str] = mapped_column(String(5))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_send_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class EvaluationRun(IdMixin, Base):
    __tablename__ = "evaluation_runs"

    dealership_id: Mapped[int | None] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"))
    engine_version: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    total_cases: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    failures: Mapped[list] = mapped_column(JSON, default=list)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    triggered_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(Text)

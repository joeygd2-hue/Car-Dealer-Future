"""Phase 4: usage and cost tracking for AI and other integrations."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import IdMixin, utcnow

COST_BASES = ("verified_billing", "estimated", "unknown")


class UsageRecord(IdMixin, Base):
    """One measured usage event.

    `quantity` is what the provider reported (e.g. tokens) — that is measured.
    `cost_micros` is either copied from provider billing (`verified_billing`),
    computed from our rate table (`estimated`) or not known (`unknown`, NULL cost).
    """

    __tablename__ = "usage_records"
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    service_category: Mapped[str] = mapped_column(String(30), nullable=False)
    metric: Mapped[str] = mapped_column(String(60), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_micros: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    cost_basis: Mapped[str] = mapped_column(String(20), nullable=False)
    rate_id: Mapped[int | None] = mapped_column(ForeignKey("provider_rates.id", ondelete="SET NULL"))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"), index=True)
    feature: Mapped[str | None] = mapped_column(String(60))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_usage_qty"),
        CheckConstraint("cost_basis in ('verified_billing','estimated','unknown')", name="ck_usage_basis"),
        CheckConstraint(
            "service_category in ('ai','email','data_api','messaging','hosting','calendar','seo','other')",
            name="ck_usage_category",
        ),
    )


class ProviderRate(IdMixin, Base):
    """Unit price used for estimates. `verified_on` records when it was checked against the provider."""

    __tablename__ = "provider_rates"
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    metric: Mapped[str] = mapped_column(String(60), nullable=False)
    # Price per `per_units` units, in micro-currency units.
    price_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    per_units: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(500))
    verified_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)


class Budget(IdMixin, Base):
    __tablename__ = "budgets"
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(60))
    service_category: Mapped[str | None] = mapped_column(String(30))
    period: Mapped[str] = mapped_column(String(10), default="monthly", nullable=False)  # daily | monthly
    amount_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_threshold_percent: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    # Alert when a day's usage exceeds N x the trailing 7-day daily average.
    spike_multiplier: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    __table_args__ = (CheckConstraint("period in ('daily','monthly')", name="ck_budget_period"),)


class Alert(IdMixin, Base):
    __tablename__ = "alerts"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), default="warning", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

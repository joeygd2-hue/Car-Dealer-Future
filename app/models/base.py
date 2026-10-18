"""Shared model helpers.

Conventions (see docs/architecture/shared-contracts.md):
* Money is stored as integer minor units (cents) with an explicit ISO currency.
* Fractional provider costs (AI tokens etc.) are stored as integer micro-units (1e-6).
* Datetimes are stored as naive UTC; dates are calendar dates in the business timezone.
* Every tenant-owned table has a NOT NULL, indexed `client_id`.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class IdMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=func.now(), nullable=False
    )

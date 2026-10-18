"""Persisted sandbox traffic so simulated end-to-end runs survive worker restarts and are inspectable."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.models.base import IdMixin


class SandboxRecord(IdMixin, Base):
    __tablename__ = "sandbox_records"

    # EMAIL_OUT|EMAIL_IN|CAL_EVENT|VIDEO_MEETING|CHANNEL_OUT
    kind: Mapped[str] = mapped_column(String(30), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

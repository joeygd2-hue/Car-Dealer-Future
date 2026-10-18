"""Phase 3: customer test-drive appointments (distinct from operator SalesMeeting records)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow
from app.models.base import IdMixin, TimestampMixin


class Appointment(IdMixin, TimestampMixin, Base):
    __tablename__ = "appointments"

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("customer_contacts.id", ondelete="SET NULL"))
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(30), default="TEST_DRIVE")
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str | None] = mapped_column(String(40))
    customer_email: Mapped[str | None] = mapped_column(String(200))
    requested_start: Mapped[datetime] = mapped_column(DateTime)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    confirmed_start: Mapped[datetime | None] = mapped_column(DateTime)
    # REQUESTED|PENDING_CONFIRMATION|CONFIRMED|DECLINED|CANCELLED|RESCHEDULE_REQUESTED|COMPLETED|NO_SHOW
    status: Mapped[str] = mapped_column(String(30), default="REQUESTED")
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    calendar_provider: Mapped[str | None] = mapped_column(String(40))
    calendar_event_id: Mapped[str | None] = mapped_column(String(200))
    availability_checked: Mapped[str] = mapped_column(String(30), default="NOT_CHECKED")  # NOT_CHECKED|FREE|BUSY|NO_CALENDAR
    notes: Mapped[str | None] = mapped_column(Text)

    events: Mapped[list[AppointmentEvent]] = relationship(
        back_populates="appointment", order_by="AppointmentEvent.id", cascade="all, delete-orphan"
    )


class AppointmentEvent(IdMixin, Base):
    __tablename__ = "appointment_events"

    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    appointment: Mapped[Appointment] = relationship(back_populates="events")

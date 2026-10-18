"""Dealership customer records (end customers of a client dealership).

Not to be confused with outreach ``Prospect`` (dealerships we sell services to).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import IdMixin, TimestampMixin


class CustomerContact(IdMixin, TimestampMixin, Base):
    __tablename__ = "customer_contacts"

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    preferred_language: Mapped[str | None] = mapped_column(String(5))  # en|es
    # How contact details were collected (customer-provided in chat, ADF lead form, etc.)
    contact_source: Mapped[str | None] = mapped_column(String(100))
    sms_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    sms_consent_source: Mapped[str | None] = mapped_column(Text)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)

    identities: Mapped[list[CustomerIdentity]] = relationship(back_populates="contact", cascade="all, delete-orphan")


class CustomerIdentity(IdMixin, Base):
    """Channel-specific identifier (Messenger PSID, phone number, chat visitor id…)."""

    __tablename__ = "customer_identities"
    __table_args__ = (UniqueConstraint("dealership_id", "channel", "external_id"),)

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("customer_contacts.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(200))

    contact: Mapped[CustomerContact] = relationship(back_populates="identities")


class CustomerLead(IdMixin, TimestampMixin, Base):
    """Sales opportunity in the dealership's CRM, linked to a conversation when one exists."""

    __tablename__ = "customer_leads"

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("customer_contacts.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(40))  # channel or ADF provider
    status: Mapped[str] = mapped_column(String(30), default="NEW")  # NEW|CONTACTED|APPOINTMENT|SOLD|LOST
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    external_ref: Mapped[str | None] = mapped_column(String(200))


class ContactSuppression(IdMixin, Base):
    """Per-dealership opt-out registry for customer messaging."""

    __tablename__ = "contact_suppressions"
    __table_args__ = (UniqueConstraint("dealership_id", "channel", "address"),)

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(40))
    address: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(40), default="OPT_OUT")
    created_at: Mapped[datetime] = mapped_column(DateTime)

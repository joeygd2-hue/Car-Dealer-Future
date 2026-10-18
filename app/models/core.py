"""Shared core models: tenants (dealerships), users, auth sessions, audit, controls, integrations.

Owned by: shared platform (all phases extend, never duplicate).
"""

from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow
from app.models.base import IdMixin, TimestampMixin


def _public_key() -> str:
    return secrets.token_urlsafe(18)


class Dealership(IdMixin, TimestampMixin, Base):
    """A client dealership (tenant). Every tenant-owned row carries ``dealership_id``."""

    __tablename__ = "dealerships"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="ONBOARDING")  # ONBOARDING|ACTIVE|SUSPENDED
    service_plan: Mapped[str | None] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago")
    public_chat_key: Mapped[str] = mapped_column(String(64), unique=True, default=_public_key)

    # Dealer-approved business information. Only used in customer replies when approved.
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(300))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(40))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    business_info_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    business_info_approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    business_info_approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    hours: Mapped[list[BusinessHours]] = relationship(
        back_populates="dealership", cascade="all, delete-orphan", order_by="BusinessHours.weekday"
    )

    @property
    def full_address(self) -> str | None:
        parts = [self.address_line1, self.city, " ".join(p for p in [self.state, self.postal_code] if p)]
        parts = [p for p in parts if p]
        return ", ".join(parts) if parts else None


class BusinessHours(IdMixin, Base):
    __tablename__ = "business_hours"
    __table_args__ = (UniqueConstraint("dealership_id", "weekday"),)

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Monday … 6=Sunday
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    open_time: Mapped[str | None] = mapped_column(String(5))  # "09:00"
    close_time: Mapped[str | None] = mapped_column(String(5))  # "19:00"

    dealership: Mapped[Dealership] = relationship(back_populates="hours")


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300))
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Membership(IdMixin, TimestampMixin, Base):
    """Links a user to a dealership with a role. Platform admins need no membership."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "dealership_id"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(30), default="DEALER_STAFF")  # DEALER_ADMIN | DEALER_STAFF
    receives_escalations: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="memberships")
    dealership: Mapped[Dealership] = relationship()


class UserSession(IdMixin, Base):
    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    user: Mapped[User] = relationship()


class AuditLog(IdMixin, Base):
    """Append-only activity log. Every external action and state transition is recorded here."""

    __tablename__ = "audit_logs"

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    dealership_id: Mapped[int | None] = mapped_column(ForeignKey("dealerships.id"), index=True)
    actor_type: Mapped[str] = mapped_column(String(20))  # USER|SYSTEM|AGENT|WEBHOOK|CUSTOMER|WORKER
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(60))
    outcome: Mapped[str] = mapped_column(String(20), default="OK")  # OK|FAILED|BLOCKED|SIMULATED
    external: Mapped[bool] = mapped_column(Boolean, default=False)  # touched a third party / customer
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class SystemControl(IdMixin, Base):
    """Global switches (e.g. ``automation.global_pause``, ``outreach.emergency_stop``)."""

    __tablename__ = "system_controls"

    key: Mapped[str] = mapped_column(String(100), unique=True)
    bool_value: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LiveAuthorization(IdMixin, Base):
    """Explicit human authorization to use a live provider for a scope (e.g. ``outreach.email``).

    Live sending requires BOTH ``LIVE_INTEGRATIONS_ENABLED=true`` and an unrevoked row here.
    """

    __tablename__ = "live_authorizations"

    scope: Mapped[str] = mapped_column(String(100), index=True)
    dealership_id: Mapped[int | None] = mapped_column(ForeignKey("dealerships.id"))
    statement: Mapped[str] = mapped_column(Text)
    authorized_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    authorized_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class IntegrationConnection(IdMixin, TimestampMixin, Base):
    """A configured provider connection. ``dealership_id`` NULL means platform-level.

    Credentials are encrypted (see app.security.crypto) and never sent to templates or prompts.
    ``paused`` is the per-channel automation pause.
    """

    __tablename__ = "integration_connections"
    __table_args__ = (UniqueConstraint("provider", "external_account_id"),)

    dealership_id: Mapped[int | None] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    external_account_id: Mapped[str] = mapped_column(String(200))  # page id, phone number, mailbox…
    display_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="NOT_CONFIGURED")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    encrypted_credentials: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

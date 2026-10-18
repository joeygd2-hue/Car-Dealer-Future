"""Core platform models: identity, tenancy, audit, jobs, integrations, support."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import IdMixin, TimestampMixin, utcnow

STAFF_ROLES = ("owner", "admin", "staff")
ADMIN_ROLES = ("owner", "admin")
ALL_ROLES = (*STAFF_ROLES, "client")


class Client(IdMixin, TimestampMixin, Base):
    """A dealership that is (or is becoming) a paying client. The tenant boundary."""

    __tablename__ = "clients"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="onboarding", nullable=False)
    address_line1: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(50))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    phone: Mapped[str | None] = mapped_column(String(40))
    website: Mapped[str | None] = mapped_column(String(300))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(254))
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago", nullable=False)
    languages: Mapped[list] = mapped_column(JSON, default=lambda: ["en", "es"], nullable=False)
    business_hours: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_rules: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"))

    __table_args__ = (
        CheckConstraint("status in ('onboarding','active','paused','cancelled')", name="ck_client_status"),
    )


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)

    client: Mapped[Client | None] = relationship()

    __table_args__ = (
        CheckConstraint("role in ('owner','admin','staff','client')", name="ck_user_role"),
        # Client users must belong to exactly one client; staff must not.
        CheckConstraint(
            "(role = 'client' AND client_id IS NOT NULL) OR (role <> 'client' AND client_id IS NULL)",
            name="ck_user_client_binding",
        ),
    )

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES


class UserSession(IdMixin, Base):
    """Server-side session. Only a SHA-256 of the cookie token is stored."""

    __tablename__ = "user_sessions"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    user: Mapped[User] = relationship()


class ApiToken(IdMixin, Base):
    """Bearer token for server-to-server API access. Only the hash is stored."""

    __tablename__ = "api_tokens"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship()


class AuditLog(IdMixin, Base):
    """Append-only record of security-relevant and business-relevant actions."""

    __tablename__ = "audit_log"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_label: Mapped[str] = mapped_column(String(100), default="system", nullable=False)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(60))
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))


class IntegrationCredential(IdMixin, TimestampMixin, Base):
    """Encrypted server-side credential. Values are never returned to the browser."""

    __tablename__ = "integration_credentials"
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    last4: Mapped[str | None] = mapped_column(String(8))
    __table_args__ = (UniqueConstraint("provider", "client_id", "name", name="uq_credential"),)


class IntegrationConnection(IdMixin, TimestampMixin, Base):
    """Connection status of an integration, globally (client_id NULL) or per client.

    `status` is only `connected` after a successful verification call
    (`verified_at`). `sandbox` means a simulated provider is in use.
    """

    __tablename__ = "integration_connections"
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="not_connected", nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime)
    authorized_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    authorization_note: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "kind", "provider", name="uq_integration_conn"),
        CheckConstraint(
            "status in ('not_connected','sandbox','connected','error','disabled')", name="ck_integration_status"
        ),
    )


class Attachment(IdMixin, Base):
    __tablename__ = "attachments"
    owner_type: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(300), nullable=False)
    uploaded_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    __table_args__ = (Index("ix_attachment_owner", "owner_type", "owner_id"),)


class Job(IdMixin, Base):
    """Durable background job. Claimed with a lease so crashed workers are recovered."""

    __tablename__ = "jobs"
    kind: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSON)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','succeeded','failed','dead','cancelled')", name="ck_job_status"
        ),
    )


class ScheduledTask(IdMixin, Base):
    """Recurring task definitions; the scheduler enqueues one job per due slot."""

    __tablename__ = "scheduled_tasks"
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    job_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    jobs_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[str | None] = mapped_column(String(40))


class WebhookEvent(IdMixin, Base):
    """Every inbound webhook, recorded once per (provider, event_id) for de-duplication."""

    __tablename__ = "webhook_events"
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_event"),)


class OutboundEmail(IdMixin, Base):
    """Every outbound email, whether delivered through a provider or held in the sandbox."""

    __tablename__ = "outbound_emails"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    to_address: Mapped[str] = mapped_column(String(254), nullable=False)
    from_address: Mapped[str | None] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(300), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)


class SupportTicket(IdMixin, TimestampMixin, Base):
    __tablename__ = "support_tickets"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    priority: Mapped[str] = mapped_column(String(10), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assignee_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "kind in ('client_issue','integration_failure','change_request','onboarding','other')",
            name="ck_ticket_kind",
        ),
        CheckConstraint("priority in ('low','normal','high','urgent')", name="ck_ticket_priority"),
        CheckConstraint("status in ('open','in_progress','waiting_client','resolved','closed')", name="ck_ticket_status"),
    )


class OnboardingStep(IdMixin, TimestampMixin, Base):
    __tablename__ = "onboarding_steps"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    completed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("client_id", "code", name="uq_onboarding_step"),
        CheckConstraint(
            "status in ('pending','in_progress','done','blocked','not_applicable')", name="ck_onboarding_status"
        ),
    )


class AutomationSetting(IdMixin, TimestampMixin, Base):
    """Per-client automation switch. Enabling is gated by onboarding prerequisites."""

    __tablename__ = "automation_settings"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    feature: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (UniqueConstraint("client_id", "feature", name="uq_automation_feature"),)

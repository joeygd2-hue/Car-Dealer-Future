"""Shared platform models used by every phase.

Phase 1 had not been committed when Phase 2 began, so these are the shared
foundation: a signed dealership is a ``Client``; platform staff and dealership
users are ``User`` rows with per-client ``ClientMembership`` roles.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import new_id, utcnow
from app.db import Base


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cli"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | paused | offboarded
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago")
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(40))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    # Link back to the CRM prospect this client was converted from (outreach).
    source_prospect_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("usr"))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300))
    # platform_admin: everything. platform_staff: agency staff; needs a
    # membership to see a dealership. None: dealership user.
    platform_role: Mapped[str | None] = mapped_column(String(30))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ClientMembership(Base):
    __tablename__ = "client_memberships"
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    # dealer_admin | dealer_reviewer | dealer_viewer | agency_operator
    role: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column()


class AuditLog(Base):
    """Append-only activity log for every phase (enforced in app/core/audit.py)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str | None] = mapped_column(String(40), index=True)
    actor_type: Mapped[str] = mapped_column(String(10))  # user | system | job | agent
    actor_id: Mapped[str | None] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(80))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


class Job(Base):
    """Durable job queue shared by inventory sync, publishing and the outreach agent."""
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("job"))
    queue: Mapped[str] = mapped_column(String(40), default="default")
    type: Mapped[str] = mapped_column(String(80))
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # queued | running | succeeded | failed | dead | cancelled
    status: Mapped[str] = mapped_column(String(12), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    run_at: Mapped[datetime] = mapped_column(default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    locked_by: Mapped[str | None] = mapped_column(String(120))
    locked_until: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (Index("ix_jobs_ready", "status", "queue", "run_at", "priority"),)


class JobSchedule(Base):
    __tablename__ = "job_schedules"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    type: Mapped[str] = mapped_column(String(80))
    queue: Mapped[str] = mapped_column(String(40), default="default")
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    next_run_at: Mapped[datetime] = mapped_column()
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_job_id: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_beat_at: Mapped[datetime] = mapped_column(default=utcnow)
    status: Mapped[str] = mapped_column(String(20))
    info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SystemFlag(Base):
    """Platform switches: global pause, emergency stop, etc."""
    __tablename__ = "system_flags"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_by: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class PlatformIntegration(Base):
    """Agency-level provider connections (outreach mailbox, calendar, places).

    Secrets are AES-GCM encrypted (app/core/crypto.py) and never returned by the API.
    """
    __tablename__ = "platform_integrations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("int"))
    kind: Mapped[str] = mapped_column(String(40))  # email | inbox | calendar | discovery | ai
    provider: Mapped[str] = mapped_column(String(40))  # sandbox | gmail | google_calendar | google_places ...
    name: Mapped[str] = mapped_column(String(120))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    # Live use requires explicit human authorisation recorded here.
    live_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized_by: Mapped[str | None] = mapped_column(String(40))
    authorized_at: Mapped[datetime | None] = mapped_column()
    health_status: Mapped[str] = mapped_column(String(20), default="unknown")  # ok | degraded | down | unknown
    health_detail: Mapped[str | None] = mapped_column(Text)
    health_checked_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("kind", "name"),)

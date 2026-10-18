"""Phase 2 listing generation, approval and publishing models."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import new_id, utcnow
from app.db import Base, TenantMixin

LISTING_STATUSES = (
    "imported", "needs_validation", "draft_generated", "awaiting_approval",
    "approved", "rejected", "sold_withdrawn",
)
PUBLICATION_STATUSES = (
    "ready_to_publish", "submitted", "published_confirmed", "manual_required",
    "failed", "withdrawal_required", "withdrawn",
)


class DealerListingSettings(TenantMixin, Base):
    __tablename__ = "dealer_listing_settings"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("dls"))
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("client_id", name="uq_dls_client"),
                      ForeignKeyConstraint(["client_id"], ["clients.id"]))


class Listing(TenantMixin, Base):
    __tablename__ = "listings"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("lst"))
    vehicle_id: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="imported")
    current_revision_id: Mapped[str | None] = mapped_column(String(40))
    approved_revision_id: Mapped[str | None] = mapped_column(String(40))
    approved_by: Mapped[str | None] = mapped_column(String(40))
    approved_at: Mapped[datetime | None] = mapped_column()
    content_outdated: Mapped[bool] = mapped_column(Boolean, default=False)
    outdated_reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_listings_client_id"),
        UniqueConstraint("client_id", "vehicle_id", name="uq_listings_vehicle"),
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"]),
        Index("ix_listings_status", "client_id", "status"),
    )


class ListingRevision(TenantMixin, Base):
    """Immutable snapshot of generated or edited content plus the exact facts it used."""
    __tablename__ = "listing_revisions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rev"))
    listing_id: Mapped[str] = mapped_column(String(40), index=True)
    revision_number: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(30))  # template_generator | ai_generator | human_edit
    generator_model: Mapped[str | None] = mapped_column(String(80))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON)
    facts_hash: Mapped[str] = mapped_column(String(64))
    vehicle_version: Mapped[int] = mapped_column(Integer)
    settings_version: Mapped[int] = mapped_column(Integer)
    guardrails: Mapped[dict[str, Any]] = mapped_column(JSON, default=lambda: {"errors": [], "warnings": []})
    uncertainty: Mapped[list[Any]] = mapped_column(JSON, default=list)
    feedback: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_revisions_client_id"),
        UniqueConstraint("listing_id", "revision_number", name="uq_revision_number"),
        ForeignKeyConstraint(["client_id", "listing_id"], ["listings.client_id", "listings.id"]),
    )


class ListingReview(TenantMixin, Base):
    __tablename__ = "listing_reviews"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("lrv"))
    listing_id: Mapped[str] = mapped_column(String(40), index=True)
    revision_id: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str] = mapped_column(String(30))  # approved | rejected | regeneration_requested
    note: Mapped[str | None] = mapped_column(Text)
    acknowledged_warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    batch_id: Mapped[str | None] = mapped_column(String(40))
    user_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["client_id", "listing_id"], ["listings.client_id", "listings.id"]),
        ForeignKeyConstraint(["client_id", "revision_id"], ["listing_revisions.client_id", "listing_revisions.id"]),
    )


class ChannelConnection(TenantMixin, Base):
    """A dealership's credentials/config for one publishing channel (encrypted)."""
    __tablename__ = "channel_connections"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("chc"))
    channel: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="configured")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    live_publishing: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized_by: Mapped[str | None] = mapped_column(String(40))
    authorized_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("client_id", "channel", name="uq_connection_channel"),
                      ForeignKeyConstraint(["client_id"], ["clients.id"]))


class ChannelPublication(TenantMixin, Base):
    """One listing on one channel. Unique per (listing, channel): no duplicates."""
    __tablename__ = "channel_publications"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pub"))
    listing_id: Mapped[str] = mapped_column(String(40))
    vehicle_id: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30))
    needs_update: Mapped[bool] = mapped_column(Boolean, default=False)
    update_reason: Mapped[str | None] = mapped_column(String(300))
    external_id: Mapped[str | None] = mapped_column(String(200))
    external_url: Mapped[str | None] = mapped_column(Text)
    published_revision_id: Mapped[str | None] = mapped_column(String(40))
    published_vehicle_version: Mapped[int | None] = mapped_column(Integer)
    confirmation_type: Mapped[str | None] = mapped_column(String(20))  # api | manual_attested
    confirmed_by: Mapped[str | None] = mapped_column(String(40))
    confirmed_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_publications_client_id"),
        UniqueConstraint("client_id", "listing_id", "channel", name="uq_publication_channel"),
        ForeignKeyConstraint(["client_id", "listing_id"], ["listings.client_id", "listings.id"]),
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"]),
        Index("ix_publications_status", "client_id", "channel", "status"),
    )


class PublishOperation(TenantMixin, Base):
    """Every attempted channel operation, keyed for idempotency."""
    __tablename__ = "publish_operations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pop"))
    publication_id: Mapped[str] = mapped_column(String(40), index=True)
    # create | update | delete | status_check | prepare_manual | confirm_manual
    # | record_manual_update | record_manual_removal
    operation: Mapped[str] = mapped_column(String(30))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))  # pending | succeeded | failed | dead
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_publish_op_key"),
        ForeignKeyConstraint(["client_id", "publication_id"], ["channel_publications.client_id", "channel_publications.id"]),
    )

"""Phase 2 models: dealership clients, inventory, listings and publishing.

Tenant isolation (a dealership's data must never reach another dealership):

- Every dealership-owned table inherits ``TenantMixin`` (``client_id NOT NULL``).
- Child rows reference parents through composite ``(client_id, parent_id)`` foreign keys and each
  parent declares ``UniqueConstraint("client_id", "id")``, so the database itself refuses a row that
  points at another dealership's parent.
- Sessions opened with ``tenancy.tenant_session(client_id)`` automatically filter every query on
  tenant models and refuse to write rows for another client (see ``dealerflow/tenancy.py``).

Timestamps are naive UTC like the rest of dealerflow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from .db import Base, utcnow
from .models_ops import Client  # noqa: F401  (shared tenant table; re-exported for Phase 2 code)

__all__ = [
    "CLIENT_ROLES", "SOURCE_TYPES", "LISTING_STATUSES", "PUBLICATION_STATUSES", "TenantMixin", "Client",
    "InventorySource", "ImportRun", "ImportRawRecord", "Vehicle", "VehiclePhoto", "VehicleChange",
    "DealerListingSettings", "Listing", "ListingRevision", "ListingReview", "ChannelConnection",
    "ChannelPublication", "PublishOperation",
]

CLIENT_ROLES = ("dealer_admin", "dealer_reviewer", "dealer_viewer")
SOURCE_TYPES = ("csv_upload", "spreadsheet_upload", "dms_export", "feed_api", "website_feed", "manual")
LISTING_STATUSES = ("imported", "needs_validation", "draft_generated", "awaiting_approval", "approved", "rejected",
                    "sold_withdrawn")
PUBLICATION_STATUSES = ("ready_to_publish", "submitted", "published_confirmed", "manual_required", "failed",
                        "withdrawal_required", "withdrawn")


class TenantMixin:
    """Marks a table as owned by one dealership client."""

    @declared_attr
    def client_id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)




class InventorySource(TenantMixin, Base):
    __tablename__ = "inventory_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # feed URL, format, records path, column_map, auth type. Never secrets: a feed credential is
    # read from the environment variable named in ``secret_env`` (dealerflow keeps secrets in env only).
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    secret_env: Mapped[str | None] = mapped_column(String(80))
    full_snapshot: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    stale_after_hours: Mapped[int] = mapped_column(Integer, default=72, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    authorization_note: Mapped[str | None] = mapped_column(Text)
    last_run_id: Mapped[int | None] = mapped_column(Integer)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("client_id", "id", name="uq_inventory_sources_client_id"),)


class ImportRun(TenantMixin, Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)  # upload | schedule | manual | api
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # running | succeeded | partial | failed
    file_name: Mapped[str | None] = mapped_column(String(300))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_import_runs_client_id"),
        ForeignKeyConstraint(["client_id", "source_id"], ["inventory_sources.client_id", "inventory_sources.id"],
                             name="fk_import_runs_source"),
    )


class ImportRawRecord(TenantMixin, Base):
    """Each source row exactly as received."""

    __tablename__ = "import_raw_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120))
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)  # created|updated|unchanged|rejected|duplicate_in_file
    vehicle_id: Mapped[int | None] = mapped_column(Integer)
    issues: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["client_id", "run_id"], ["import_runs.client_id", "import_runs.id"], name="fk_raw_records_run"),
    )


class Vehicle(TenantMixin, Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    vin: Mapped[str | None] = mapped_column(String(17), index=True)
    stock_number: Mapped[str | None] = mapped_column(String(60))
    year: Mapped[int | None] = mapped_column(Integer)
    make: Mapped[str | None] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(80))
    trim: Mapped[str | None] = mapped_column(String(120))
    body_style: Mapped[str | None] = mapped_column(String(60))
    mileage: Mapped[int | None] = mapped_column(Integer)
    price_cents: Mapped[int | None] = mapped_column(Integer)
    condition: Mapped[str | None] = mapped_column(String(30))  # used | new | certified_pre_owned
    exterior_color: Mapped[str | None] = mapped_column(String(60))
    interior_color: Mapped[str | None] = mapped_column(String(60))
    transmission: Mapped[str | None] = mapped_column(String(30))
    drivetrain: Mapped[str | None] = mapped_column(String(10))
    fuel_type: Mapped[str | None] = mapped_column(String(30))
    engine: Mapped[str | None] = mapped_column(String(120))
    features: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    location_name: Mapped[str | None] = mapped_column(String(200))
    location_address: Mapped[str | None] = mapped_column(String(300))
    location_city: Mapped[str | None] = mapped_column(String(120))
    location_state: Mapped[str | None] = mapped_column(String(40))
    location_postal: Mapped[str | None] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(Text)
    # Dealer-supplied history facts. NULL = not supplied: never rendered as a claim either way.
    title_status: Mapped[str | None] = mapped_column(String(30))
    owner_count: Mapped[int | None] = mapped_column(Integer)
    inventory_status: Mapped[str] = mapped_column(String(20), default="available", nullable=False)
    # Phase 3: last time a person (or an authorised feed sync) verified price/availability, and a note when
    # sources disagree. The customer-response engine quotes price/availability only while fresh and unconflicted.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    data_conflict_note: Mapped[str | None] = mapped_column(Text)
    validation_status: Mapped[str] = mapped_column(String(20), default="valid", nullable=False)
    validation_issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    photo_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_vehicles_client_id"),
        UniqueConstraint("client_id", "source_id", "external_id", name="uq_vehicles_source_external"),
        ForeignKeyConstraint(["client_id", "source_id"], ["inventory_sources.client_id", "inventory_sources.id"],
                             name="fk_vehicles_source"),
        Index("ix_vehicles_client_status", "client_id", "inventory_status"),
    )


class VehiclePhoto(TenantMixin, Base):
    """A reference to a dealer-hosted photo. We store URLs, not image bytes."""

    __tablename__ = "vehicle_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"], name="fk_vehicle_photos_vehicle"),
    )


class VehicleChange(TenantMixin, Base):
    """Synchronization history: one row per detected change."""

    __tablename__ = "vehicle_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer)
    # created | price_changed | details_changed | photos_changed | status_changed | sold | removed | reappeared | stale
    change_type: Mapped[str] = mapped_column(String(30), nullable=False)
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"], name="fk_vehicle_changes_vehicle"),
    )


class DealerListingSettings(TenantMixin, Base):
    __tablename__ = "dealer_listing_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("client_id", name="uq_dealer_listing_settings_client"),)


class Listing(TenantMixin, Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="imported", nullable=False)
    current_revision_id: Mapped[int | None] = mapped_column(Integer)
    approved_revision_id: Mapped[int | None] = mapped_column(Integer)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    content_outdated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    outdated_reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_listings_client_id"),
        UniqueConstraint("client_id", "vehicle_id", name="uq_listings_vehicle"),
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"], name="fk_listings_vehicle"),
        Index("ix_listings_client_status", "client_id", "status"),
    )


class ListingRevision(TenantMixin, Base):
    """Immutable snapshot of generated or edited content plus the exact facts it used."""

    __tablename__ = "listing_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(String(30), nullable=False)  # template_generator | ai_generator | human_edit
    generator_model: Mapped[str | None] = mapped_column(String(80))
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    facts_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vehicle_version: Mapped[int] = mapped_column(Integer, nullable=False)
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False)
    guardrails: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    uncertainty: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    feedback: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_by_label: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_listing_revisions_client_id"),
        UniqueConstraint("listing_id", "revision_number", name="uq_listing_revision_number"),
        ForeignKeyConstraint(["client_id", "listing_id"], ["listings.client_id", "listings.id"], name="fk_listing_revisions_listing"),
    )


class ListingReview(TenantMixin, Base):
    __tablename__ = "listing_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revision_id: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)  # approved | rejected | regeneration_requested
    note: Mapped[str | None] = mapped_column(Text)
    acknowledged_warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(40))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["client_id", "listing_id"], ["listings.client_id", "listings.id"], name="fk_listing_reviews_listing"),
        ForeignKeyConstraint(["client_id", "revision_id"], ["listing_revisions.client_id", "listing_revisions.id"],
                             name="fk_listing_reviews_revision"),
    )


class ChannelConnection(TenantMixin, Base):
    """A dealership's configuration for one publishing channel. The credential is read from the
    environment variable named in ``token_env``; no secret is stored in the database."""

    __tablename__ = "channel_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    token_env: Mapped[str | None] = mapped_column(String(80))
    live_publishing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    authorized_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("client_id", "channel", name="uq_channel_connection"),)


class ChannelPublication(TenantMixin, Base):
    """One listing on one channel. Unique per (listing, channel): duplicates are impossible."""

    __tablename__ = "channel_publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vehicle_id: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    needs_update: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    update_reason: Mapped[str | None] = mapped_column(String(300))
    external_id: Mapped[str | None] = mapped_column(String(200))
    external_url: Mapped[str | None] = mapped_column(Text)
    published_revision_id: Mapped[int | None] = mapped_column(Integer)
    published_vehicle_version: Mapped[int | None] = mapped_column(Integer)
    confirmation_type: Mapped[str | None] = mapped_column(String(20))  # api | manual_attested
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_channel_publications_client_id"),
        UniqueConstraint("client_id", "listing_id", "channel", name="uq_publication_channel"),
        ForeignKeyConstraint(["client_id", "listing_id"], ["listings.client_id", "listings.id"], name="fk_publications_listing"),
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"], name="fk_publications_vehicle"),
        Index("ix_publications_client_channel_status", "client_id", "channel", "status"),
    )


class PublishOperation(TenantMixin, Base):
    """Every attempted channel operation, keyed for idempotency."""

    __tablename__ = "publish_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # create | update | delete | status_check | prepare_manual | confirm_manual | record_manual_update | record_manual_removal
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # pending | succeeded | failed | dead
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_publish_operation_key"),
        ForeignKeyConstraint(["client_id", "publication_id"], ["channel_publications.client_id", "channel_publications.id"],
                             name="fk_publish_operations_publication"),
    )

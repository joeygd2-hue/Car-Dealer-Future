"""Phase 2 inventory models. Every table is dealership-scoped (TenantMixin) and
children use composite (client_id, parent_id) foreign keys."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import new_id, utcnow
from app.db import Base, TenantMixin

SOURCE_TYPES = ("csv_upload", "spreadsheet_upload", "dms_export", "feed_api", "website_feed", "manual")
INVENTORY_STATUSES = ("available", "pending", "sold", "removed", "unknown")


class InventorySource(TenantMixin, Base):
    __tablename__ = "inventory_sources"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("src"))
    name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(30))
    # mapping profile, feed URL, format, header overrides; never secrets.
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Encrypted feed credential (e.g. an API token the dealer issued to us).
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    # If true, vehicles missing from a successful import are marked removed.
    full_snapshot: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    stale_after_hours: Mapped[int] = mapped_column(Integer, default=72)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Who at the dealership authorised this source and how (required for feeds).
    authorization_note: Mapped[str | None] = mapped_column(Text)
    last_run_id: Mapped[str | None] = mapped_column(String(40))
    last_sync_at: Mapped[datetime | None] = mapped_column()
    last_status: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_sources_client_id"),
        ForeignKeyConstraint(["client_id"], ["clients.id"]),
    )


class ImportRun(TenantMixin, Base):
    __tablename__ = "import_runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("run"))
    source_id: Mapped[str] = mapped_column(String(40))
    trigger: Mapped[str] = mapped_column(String(20))  # upload | schedule | manual | api
    status: Mapped[str] = mapped_column(String(20))  # running | succeeded | partial | failed
    file_name: Mapped[str | None] = mapped_column(String(300))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_runs_client_id"),
        ForeignKeyConstraint(["client_id", "source_id"], ["inventory_sources.client_id", "inventory_sources.id"]),
    )


class ImportRawRecord(TenantMixin, Base):
    """Source rows preserved exactly as received."""
    __tablename__ = "import_raw_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(120))
    raw: Mapped[dict[str, Any]] = mapped_column(JSON)
    # created | updated | unchanged | rejected | duplicate_in_file
    outcome: Mapped[str] = mapped_column(String(20))
    vehicle_id: Mapped[str | None] = mapped_column(String(40))
    issues: Mapped[list[Any]] = mapped_column(JSON, default=list)
    __table_args__ = (
        ForeignKeyConstraint(["client_id", "run_id"], ["import_runs.client_id", "import_runs.id"]),
    )


class Vehicle(TenantMixin, Base):
    __tablename__ = "vehicles"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("veh"))
    source_id: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(120))
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
    features: Mapped[list[str]] = mapped_column(JSON, default=list)
    location_name: Mapped[str | None] = mapped_column(String(200))
    location_address: Mapped[str | None] = mapped_column(String(300))
    location_city: Mapped[str | None] = mapped_column(String(120))
    location_state: Mapped[str | None] = mapped_column(String(40))
    location_postal: Mapped[str | None] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(Text)
    # Dealer-supplied history facts. None = not supplied: never render a claim.
    title_status: Mapped[str | None] = mapped_column(String(30))
    owner_count: Mapped[int | None] = mapped_column(Integer)
    inventory_status: Mapped[str] = mapped_column(String(20), default="available")
    validation_status: Mapped[str] = mapped_column(String(20), default="valid")  # valid | warnings | invalid
    validation_issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    photo_fingerprint: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_synced_at: Mapped[datetime] = mapped_column(default=utcnow)
    sold_at: Mapped[datetime | None] = mapped_column()
    removed_at: Mapped[datetime | None] = mapped_column()
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_vehicles_client_id"),
        UniqueConstraint("client_id", "source_id", "external_id", name="uq_vehicles_source_ext"),
        ForeignKeyConstraint(["client_id", "source_id"], ["inventory_sources.client_id", "inventory_sources.id"]),
        Index("ix_vehicles_status", "client_id", "inventory_status"),
    )


class VehiclePhoto(TenantMixin, Base):
    """A reference to a dealer-hosted photo. We store URLs, not image bytes."""
    __tablename__ = "vehicle_photos"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pho"))
    vehicle_id: Mapped[str] = mapped_column(String(40), index=True)
    url: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)
    caption: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"]),
    )


class VehicleChange(TenantMixin, Base):
    """Synchronization history: one row per detected change."""
    __tablename__ = "vehicle_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40))
    # created | price_changed | details_changed | photos_changed | status_changed
    # | sold | removed | reappeared | stale | manual_edit
    change_type: Mapped[str] = mapped_column(String(30))
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["client_id", "vehicle_id"], ["vehicles.client_id", "vehicles.id"]),
    )

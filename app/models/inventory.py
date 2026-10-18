"""Vehicle inventory + listing drafts.

Minimal shared base created in Phase 3 because Phase 2 code was not present in the repository.
Phase 2 owns and should EXTEND these tables (ingestion, photos, publishing) rather than duplicate them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import IdMixin, TimestampMixin


class Vehicle(IdMixin, TimestampMixin, Base):
    __tablename__ = "vehicles"
    __table_args__ = (UniqueConstraint("dealership_id", "vin"),)

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    vin: Mapped[str | None] = mapped_column(String(17))
    stock_number: Mapped[str | None] = mapped_column(String(50))
    year: Mapped[int | None] = mapped_column(Integer)
    make: Mapped[str | None] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(60))
    trim: Mapped[str | None] = mapped_column(String(80))
    body_style: Mapped[str | None] = mapped_column(String(60))
    exterior_color: Mapped[str | None] = mapped_column(String(60))
    interior_color: Mapped[str | None] = mapped_column(String(60))
    mileage: Mapped[int | None] = mapped_column(Integer)
    price_cents: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="UNKNOWN")  # AVAILABLE|PENDING|SOLD|UNKNOWN
    # Dealer-approved features only; the response engine never mentions unapproved features.
    approved_features: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(40), default="MANUAL")  # MANUAL|CSV|FEED|...
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Last time a human or trusted feed verified price/availability.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Non-null when sources disagree (e.g. feed price != website price). Forces escalation.
    data_conflict_note: Mapped[str | None] = mapped_column(Text)
    listing_url: Mapped[str | None] = mapped_column(String(400))

    @property
    def title(self) -> str:
        return " ".join(str(p) for p in [self.year, self.make, self.model, self.trim] if p) or "vehicle"


class ListingDraft(IdMixin, TimestampMixin, Base):
    __tablename__ = "listing_drafts"

    dealership_id: Mapped[int] = mapped_column(ForeignKey("dealerships.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    language: Mapped[str] = mapped_column(String(5), default="en")
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT|APPROVED|REJECTED
    generated_by: Mapped[str | None] = mapped_column(String(60))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str | None] = mapped_column(Text)
    published: Mapped[bool] = mapped_column(Boolean, default=False)

    vehicle: Mapped[Vehicle] = relationship()

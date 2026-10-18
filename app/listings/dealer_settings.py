"""Per-dealership listing configuration (validated with Pydantic)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.audit import audit_ctx
from app.core.tenancy import ClientContext
from app.models import Client, DealerListingSettings


class Bilingual(BaseModel):
    en: str = ""
    es: str = ""


class Contact(BaseModel):
    dealership_name: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    address: str = ""


class ChannelLimits(BaseModel):
    title: int = 100
    description: int = 5000


class ReviewRules(BaseModel):
    require_approval: bool = True
    # Agency operators may approve only if the dealership explicitly allows it.
    allow_agency_approval: bool = False
    require_both_languages: bool = True
    auto_submit_for_approval: bool = True
    max_batch_size: int = Field(25, ge=1, le=100)


class ListingSettings(BaseModel):
    brand_voice: Literal["professional", "friendly", "straightforward"] = "professional"
    brand_voice_notes: str = ""
    contact: Contact = Contact()
    business_hours: Bilingual = Bilingual()
    languages: list[Literal["en", "es"]] = ["en", "es"]
    # Approved disclaimers, inserted verbatim. Guardrails require their presence.
    disclaimers: list[Bilingual] = []
    # Optional approved financing language; financing is never mentioned otherwise.
    financing_statement: Bilingual = Bilingual()
    title_template: str = "{year} {make} {model} {trim}"
    call_to_action: Bilingual = Bilingual()
    # Conservative defaults; Meta catalog allows title<=500/description<=5000 (see docs).
    character_limits: dict[str, ChannelLimits] = {
        "default": ChannelLimits(title=100, description=5000),
        "facebook_marketplace": ChannelLimits(title=100, description=5000),
        "meta_catalog": ChannelLimits(title=150, description=5000),
    }
    required_fields: list[str] = ["year", "make", "model", "price", "mileage", "photos"]
    review: ReviewRules = ReviewRules()
    show_vin: bool = True
    show_stock_number: bool = True
    # Dealer-written free text may contain unverifiable claims; off by default.
    use_dealer_description: bool = False
    auto_generate_on_import: bool = True
    auto_regenerate_on_change: bool = True
    enabled_channels: list[str] = ["facebook_marketplace"]


def get_settings_row(ctx: ClientContext) -> DealerListingSettings:
    row = ctx.db.scalar(select(DealerListingSettings))
    if not row:
        client = ctx.db.get(Client, ctx.client_id)
        defaults = ListingSettings(contact=Contact(dealership_name=client.name if client else ""))
        row = DealerListingSettings(client_id=ctx.client_id, settings=defaults.model_dump(), version=1)
        ctx.db.add(row)
        ctx.db.flush()
    return row


def get_settings(ctx: ClientContext) -> tuple[ListingSettings, int]:
    row = get_settings_row(ctx)
    return ListingSettings.model_validate(row.settings), row.version


def update_settings(ctx: ClientContext, patch: dict) -> tuple[ListingSettings, int]:
    row = get_settings_row(ctx)
    merged = {**row.settings, **patch}
    validated = ListingSettings.model_validate(merged)
    row.settings = validated.model_dump()
    row.version += 1
    row.updated_by = ctx.actor.id
    audit_ctx(ctx, "listing_settings.updated", "dealer_listing_settings", row.id, changed=sorted(patch.keys()), version=row.version)
    ctx.db.flush()
    return validated, row.version

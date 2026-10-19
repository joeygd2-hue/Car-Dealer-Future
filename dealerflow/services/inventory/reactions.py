"""How inventory changes propagate to listings and channel publications."""
from __future__ import annotations

from sqlalchemy import select

from ...audit import audit
from ...jobs import enqueue
from ...models_dealer import ChannelPublication, Vehicle
from ...tenancy import ClientContext
from ..listings.dealer_settings import get_settings
from ..listings.service import ensure_listing, mark_outdated
from ..publishing.service import withdraw

CONTENT_CHANGES = {"price_changed", "details_changed", "photos_changed"}


def _live_pubs(ctx: ClientContext, listing_id: int) -> list[ChannelPublication]:
    return ctx.db.scalars(select(ChannelPublication).where(ChannelPublication.listing_id == listing_id,
                                                           ChannelPublication.status.not_in(("withdrawn",)))).all()


def react_to_vehicle_events(ctx: ClientContext, events: list[tuple[int, list[str]]]) -> None:
    if not events:
        return
    s, _ = get_settings(ctx)
    for vehicle_id, changes in events:
        v = ctx.db.get(Vehicle, vehicle_id)
        lst = ensure_listing(ctx, v)
        inactive = v.inventory_status in ("sold", "removed")
        if inactive:
            if lst.status != "sold_withdrawn":
                lst.status = "sold_withdrawn"
                audit(ctx.db, "listing.sold_withdrawn", actor_id=ctx.actor_id, entity_type="listing", entity_id=lst.id,
                      data={"client_id": ctx.client_id, "vehicle_status": v.inventory_status})
            for pub in _live_pubs(ctx, lst.id):
                withdraw(ctx, pub, f"vehicle_{v.inventory_status}")
            continue
        if "reappeared" in changes and lst.status == "sold_withdrawn":
            lst.status = "imported"
            lst.approved_revision_id = None
        if v.validation_status == "invalid":
            lst.status = "needs_validation"
        elif lst.status == "needs_validation":
            lst.status = "imported" if not lst.current_revision_id else "draft_generated"
        if ("created" in changes or "reappeared" in changes or lst.status == "imported") and v.validation_status != "invalid":
            if (s.auto_generate_on_import and not lst.current_revision_id) or "reappeared" in changes:
                enqueue(ctx.db, "listing_generate", f"listing_generate:{lst.id}:v{v.version}",
                        {"client_id": ctx.client_id, "listing_id": lst.id})
        if CONTENT_CHANGES & set(changes):
            reason = ", ".join(sorted(CONTENT_CHANGES & set(changes))).replace("_", " ")
            mark_outdated(ctx, v, reason, regenerate=s.auto_regenerate_on_change)
            for pub in _live_pubs(ctx, lst.id):
                if pub.status in ("published_confirmed", "submitted", "manual_required"):
                    pub.needs_update, pub.update_reason = True, f"Inventory changed ({reason}); update the live listing"
        if "status_changed" in changes and v.inventory_status == "pending":
            for pub in _live_pubs(ctx, lst.id):
                if pub.status == "published_confirmed":
                    pub.needs_update, pub.update_reason = True, "Vehicle is pending sale; consider marking it unavailable"
    ctx.db.flush()

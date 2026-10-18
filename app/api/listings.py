from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import client_ctx
from app.core.audit import audit_ctx
from app.core.errors import BadRequest
from app.core.tenancy import ClientContext
from app.listings import service
from app.listings.dealer_settings import get_settings, update_settings
from app.models import AuditLog, ChannelPublication, Listing, ListingReview, ListingRevision, Vehicle
from app.publishing import service as pub
from app.publishing.adapters import ADAPTERS
from app.reporting.metrics import dealership_metrics

router = APIRouter(prefix="/api/clients/{client_id}", tags=["listings"])


@router.get("/listing-settings")
def read_settings(ctx: ClientContext = Depends(client_ctx())):
    s, version = get_settings(ctx)
    return {"settings": s.model_dump(), "version": version}


@router.put("/listing-settings")
def write_settings(body: dict[str, Any], ctx: ClientContext = Depends(client_ctx("manage_settings"))):
    s, version = update_settings(ctx, body)
    return {"settings": s.model_dump(), "version": version}


@router.get("/listings")
def list_listings(status: str | None = None, limit: int = 100, offset: int = 0, ctx: ClientContext = Depends(client_ctx())):
    stmt = select(Listing, Vehicle).join(Vehicle, Vehicle.id == Listing.vehicle_id)
    if status:
        stmt = stmt.where(Listing.status == status)
    total = ctx.db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = []
    for lst, v in ctx.db.execute(stmt.order_by(Listing.updated_at.desc()).limit(min(limit, 500)).offset(offset)).all():
        rev = service.current_revision(ctx, lst)
        pubs = ctx.db.scalars(select(ChannelPublication).where(ChannelPublication.listing_id == lst.id)).all()
        items.append({"id": lst.id, "status": lst.status, "content_outdated": lst.content_outdated,
                      "vehicle": {"id": v.id, "year": v.year, "make": v.make, "model": v.model, "trim": v.trim,
                                  "price": v.price_cents / 100 if v.price_cents else None, "mileage": v.mileage,
                                  "inventory_status": v.inventory_status, "validation_status": v.validation_status, "stale": v.stale},
                      "title": (rev.content.get("en") or {}).get("title") if rev else None,
                      "errors": len(rev.guardrails["errors"]) if rev else 0, "warnings": len(rev.guardrails["warnings"]) if rev else 0,
                      "publications": [{"id": p.id, "channel": p.channel, "status": p.status, "needs_update": p.needs_update}
                                       for p in pubs]})
    return {"total": total, "items": items}


@router.get("/listings/{listing_id}")
def get_listing(listing_id: str, ctx: ClientContext = Depends(client_ctx())):
    lst = service.get_listing(ctx, listing_id)
    out = service.listing_view(ctx, lst)
    out["publications"] = [pub.publication_view(ctx, p) for p in ctx.db.scalars(
        select(ChannelPublication).where(ChannelPublication.listing_id == lst.id))]
    out["reviews"] = [{"decision": r.decision, "note": r.note, "user_id": r.user_id, "revision_id": r.revision_id,
                       "at": r.created_at.isoformat()} for r in ctx.db.scalars(
        select(ListingReview).where(ListingReview.listing_id == lst.id).order_by(ListingReview.created_at.desc()))]
    return out


@router.get("/listings/{listing_id}/revisions")
def revisions(listing_id: str, ctx: ClientContext = Depends(client_ctx())):
    lst = service.get_listing(ctx, listing_id)
    return [{"id": r.id, "number": r.revision_number, "origin": r.origin, "model": r.generator_model, "created_by": r.created_by,
             "created_at": r.created_at.isoformat(), "facts_hash": r.facts_hash, "vehicle_version": r.vehicle_version,
             "guardrails": r.guardrails, "uncertainty": r.uncertainty, "feedback": r.feedback, "content": r.content,
             "is_current": r.id == lst.current_revision_id, "is_approved": r.id == lst.approved_revision_id}
            for r in ctx.db.scalars(select(ListingRevision).where(ListingRevision.listing_id == lst.id)
                                    .order_by(ListingRevision.revision_number.desc()))]


class GenerateIn(BaseModel):
    feedback: str | None = None
    use_ai: bool | None = None


@router.post("/listings/{listing_id}/generate")
def generate(listing_id: str, body: GenerateIn | None = None, ctx: ClientContext = Depends(client_ctx("generate"))):
    body = body or GenerateIn()
    service.generate_listing(ctx, listing_id, feedback=body.feedback, use_ai=body.use_ai)
    return service.listing_view(ctx, service.get_listing(ctx, listing_id))


class EditIn(BaseModel):
    changes: dict[str, dict[str, Any]]
    note: str | None = None


@router.post("/listings/{listing_id}/edit")
def edit(listing_id: str, body: EditIn, ctx: ClientContext = Depends(client_ctx("edit_listing"))):
    service.edit_listing(ctx, listing_id, body.changes, body.note)
    return service.listing_view(ctx, service.get_listing(ctx, listing_id))


class ApproveIn(BaseModel):
    revision_id: str
    acknowledged_warnings: list[str] = []
    note: str | None = None


@router.post("/listings/{listing_id}/approve")
def approve(listing_id: str, body: ApproveIn, ctx: ClientContext = Depends(client_ctx("approve"))):
    service.approve_listing(ctx, listing_id, body.revision_id, body.acknowledged_warnings, body.note)
    return get_listing(listing_id, ctx)


class NoteIn(BaseModel):
    note: str | None = None


@router.post("/listings/{listing_id}/reject")
def reject(listing_id: str, body: NoteIn, ctx: ClientContext = Depends(client_ctx("approve"))):
    service.reject_listing(ctx, listing_id, body.note or "")
    return service.listing_view(ctx, service.get_listing(ctx, listing_id))


@router.post("/listings/{listing_id}/regenerate")
def regenerate(listing_id: str, body: NoteIn, ctx: ClientContext = Depends(client_ctx("generate"))):
    service.request_regeneration(ctx, listing_id, body.note)
    return service.listing_view(ctx, service.get_listing(ctx, listing_id))


class BatchIn(BaseModel):
    listing_ids: list[str]
    expected_count: int | None = None
    confirm: bool = False
    acknowledge_all_warnings: bool = False
    note: str | None = None


@router.post("/listings/batch/preview")
def batch_preview(body: BatchIn, ctx: ClientContext = Depends(client_ctx("approve"))):
    return service.batch_preview(ctx, body.listing_ids)


@router.post("/listings/batch/approve")
def batch_approve(body: BatchIn, ctx: ClientContext = Depends(client_ctx("approve"))):
    if body.expected_count is None:
        raise BadRequest("expected_count (from the preview) is required")
    return service.batch_approve(ctx, body.listing_ids, expected_count=body.expected_count, confirm=body.confirm,
                                 acknowledge_all_warnings=body.acknowledge_all_warnings, note=body.note)


# --- publishing ---

@router.get("/channels")
def channels(ctx: ClientContext = Depends(client_ctx())):
    out = []
    for key, a in ADAPTERS.items():
        conn = pub.get_connection(ctx, key)
        out.append({"key": key, "label": a.label, "mode": a.mode, "requires_connection": a.requires_connection, "docs": a.DOCS,
                    "connection": {"configured": True, "config": conn.config, "has_secret": bool(conn.secret_ciphertext),
                                   "live_publishing": conn.live_publishing,
                                   "authorized_at": conn.authorized_at.isoformat() if conn.authorized_at else None} if conn else None})
    return out


class ConnectionIn(BaseModel):
    config: dict[str, Any] = {}
    secret: str | None = None
    live_publishing: bool | None = None


@router.put("/channels/{channel}/connection")
def put_connection(channel: str, body: ConnectionIn, ctx: ClientContext = Depends(client_ctx("manage_connections"))):
    conn = pub.upsert_connection(ctx, channel, config=body.config, secret=body.secret, live_publishing=body.live_publishing)
    return {"channel": channel, "config": conn.config, "has_secret": bool(conn.secret_ciphertext), "live_publishing": conn.live_publishing}


@router.get("/publications")
def list_publications(status: str | None = None, channel: str | None = None, needs_update: bool | None = None,
                      ctx: ClientContext = Depends(client_ctx())):
    stmt = select(ChannelPublication)
    if status:
        stmt = stmt.where(ChannelPublication.status == status)
    if channel:
        stmt = stmt.where(ChannelPublication.channel == channel)
    if needs_update is not None:
        stmt = stmt.where(ChannelPublication.needs_update.is_(needs_update))
    return [pub.publication_view(ctx, p) for p in ctx.db.scalars(stmt.order_by(ChannelPublication.updated_at.desc()).limit(500))]


@router.get("/publications/{pub_id}")
def get_publication(pub_id: str, ctx: ClientContext = Depends(client_ctx())):
    return pub.publication_view(ctx, pub.get_publication(ctx, pub_id), with_package=True)


@router.post("/publications/{pub_id}/publish", status_code=202)
def publish(pub_id: str, ctx: ClientContext = Depends(client_ctx("publish"))):
    op = pub.request_publish(ctx, pub_id)
    return {"operation_id": op.id, "operation": op.operation, "status": op.status,
            "publication": pub.publication_view(ctx, pub.get_publication(ctx, pub_id))}


class ManualConfirmIn(BaseModel):
    url: str | None = None
    attested: bool = False
    notes: str | None = None


@router.post("/publications/{pub_id}/confirm-manual")
def confirm_manual(pub_id: str, body: ManualConfirmIn, ctx: ClientContext = Depends(client_ctx("publish"))):
    return pub.publication_view(ctx, pub.record_manual_publication(ctx, pub_id, url=body.url or "", attested=body.attested, notes=body.notes))


@router.post("/publications/{pub_id}/record-update")
def record_update(pub_id: str, body: ManualConfirmIn, ctx: ClientContext = Depends(client_ctx("publish"))):
    return pub.publication_view(ctx, pub.record_manual_update(ctx, pub_id, attested=body.attested, notes=body.notes))


@router.post("/publications/{pub_id}/record-removal")
def record_removal(pub_id: str, body: ManualConfirmIn, ctx: ClientContext = Depends(client_ctx("publish"))):
    return pub.publication_view(ctx, pub.record_manual_removal(ctx, pub_id, attested=body.attested, notes=body.notes))


@router.post("/publications/{pub_id}/retry", status_code=202)
def retry(pub_id: str, ctx: ClientContext = Depends(client_ctx("publish"))):
    op = pub.retry_publication(ctx, pub_id)
    return {"operation_id": op.id, "status": op.status}


@router.get("/exports/listings.csv")
def export_listings(lang: str = "en", ctx: ClientContext = Depends(client_ctx())):
    """Approved listings as CSV for authorised manual upload (e.g. Commerce Manager or a syndication provider)."""
    if lang not in ("en", "es"):
        raise BadRequest("lang must be en or es")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["vehicle_id", "vin", "stock_number", "year", "make", "model", "trim", "mileage", "price", "title", "description",
                "photos", "status"])
    for lst, v in ctx.db.execute(select(Listing, Vehicle).join(Vehicle, Vehicle.id == Listing.vehicle_id)
                                 .where(Listing.status == "approved", Vehicle.inventory_status.in_(("available", "pending")),
                                        Vehicle.stale.is_(False))).all():
        rev = ctx.db.get(ListingRevision, lst.approved_revision_id)
        lc = rev.content.get(lang) or {}
        w.writerow([v.id, v.vin, v.stock_number, v.year, v.make, v.model, v.trim, v.mileage,
                    v.price_cents / 100 if v.price_cents else "", lc.get("title"), lc.get("description"),
                    "|".join(rev.content.get("photos", [])), v.inventory_status])
    audit_ctx(ctx, "listings.exported", "client", ctx.client_id, lang=lang)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="approved-listings-{lang}.csv"'})


@router.get("/reports/summary")
def report(days: int = 30, ctx: ClientContext = Depends(client_ctx())):
    return dealership_metrics(ctx, days=max(1, min(days, 365)))


@router.get("/audit")
def audit_log(limit: int = 100, entity_id: str | None = None, ctx: ClientContext = Depends(client_ctx())):
    stmt = select(AuditLog).where(AuditLog.client_id == ctx.client_id)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    return [{"id": a.id, "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id, "actor_type": a.actor_type,
             "actor_id": a.actor_id, "details": a.details, "at": a.created_at.isoformat()}
            for a in ctx.db.scalars(stmt.order_by(AuditLog.id.desc()).limit(min(limit, 500)))]

"""Phase 2 JSON API (``/api/v1/dealers/{client_id}/...``). Auth: session cookie or ``Authorization: Bearer <api key>``.

Same authorisation as the pages (``dealer_ctx``). Another dealership's ids return 404.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select

from ..config import get_settings
from ..models_dealer import ChannelPublication, ImportRawRecord, ImportRun, Listing, Vehicle, VehiclePhoto
from ..services.dealer_errors import BadRequest, NotFound
from ..services.dealer_reports import dealership_metrics
from ..services.listings import service as listings
from ..services.publishing import service as publishing
from ..tenancy import ClientContext
from .deps import check_csrf
from .routes_dealers import dealer_ctx, export_listings_csv, upload_inventory

router = APIRouter(prefix="/api/v1/dealers/{client_id}", dependencies=[Depends(check_csrf)], tags=["dealers"])


def vehicle_json(ctx: ClientContext, v: Vehicle) -> dict[str, Any]:
    photos = [p.url for p in ctx.db.scalars(select(VehiclePhoto).where(VehiclePhoto.vehicle_id == v.id).order_by(VehiclePhoto.position))]
    lst = ctx.db.scalar(select(Listing).where(Listing.vehicle_id == v.id))
    return {"id": v.id, "external_id": v.external_id, "vin": v.vin, "stock_number": v.stock_number, "year": v.year, "make": v.make,
            "model": v.model, "trim": v.trim, "mileage": v.mileage, "price": v.price_cents / 100 if v.price_cents is not None else None,
            "condition": v.condition, "exterior_color": v.exterior_color, "transmission": v.transmission, "drivetrain": v.drivetrain,
            "fuel_type": v.fuel_type, "features": v.features, "photos": photos, "inventory_status": v.inventory_status,
            "validation_status": v.validation_status, "validation_issues": v.validation_issues, "stale": v.stale, "version": v.version,
            "simulated": v.simulated, "listing": {"id": lst.id, "status": lst.status} if lst else None}


@router.get("/vehicles")
def api_vehicles(status: str | None = None, limit: int = 100, offset: int = 0, ctx: ClientContext = Depends(dealer_ctx())):
    stmt = select(Vehicle)
    if status:
        stmt = stmt.where(Vehicle.inventory_status == status)
    total = ctx.db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = ctx.db.scalars(stmt.order_by(Vehicle.id).limit(min(limit, 500)).offset(offset)).all()
    return {"total": total, "items": [vehicle_json(ctx, v) for v in rows]}


@router.get("/vehicles/{vehicle_id}")
def api_vehicle(vehicle_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    v = ctx.db.get(Vehicle, vehicle_id)
    if not v or v.client_id != ctx.client_id:
        raise NotFound("Vehicle not found")
    return vehicle_json(ctx, v)


@router.post("/imports", status_code=201)
async def api_upload(request: Request, file: UploadFile = File(...), source_id: str = Form(""), column_map: str = Form(""),
                     ctx: ClientContext = Depends(dealer_ctx("import"))):
    run = upload_inventory(ctx, file.filename or "upload.csv", await file.read(get_settings().max_upload_bytes + 1),
                           file.content_type, source_id, column_map)
    return {"id": run.id, "status": run.status, "counts": run.counts, "error": run.error}


@router.get("/imports/{run_id}")
def api_import(run_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    run = ctx.db.get(ImportRun, run_id)
    if not run or run.client_id != ctx.client_id:
        raise NotFound("Import not found")
    recs = ctx.db.scalars(select(ImportRawRecord).where(ImportRawRecord.run_id == run.id).order_by(ImportRawRecord.row_number)).all()
    return {"id": run.id, "status": run.status, "counts": run.counts, "error": run.error,
            "records": [{"row": r.row_number, "external_id": r.external_id, "outcome": r.outcome, "issues": r.issues,
                         "vehicle_id": r.vehicle_id} for r in recs]}


@router.get("/listings")
def api_listings(status: str | None = None, ctx: ClientContext = Depends(dealer_ctx())):
    stmt = select(Listing)
    if status:
        stmt = stmt.where(Listing.status == status)
    return {"items": [listings.listing_view(ctx, lst, full=False) for lst in ctx.db.scalars(stmt.order_by(Listing.id))]}


@router.get("/listings/{listing_id}")
def api_listing(listing_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    lst = listings.get_listing(ctx, listing_id)
    out = listings.listing_view(ctx, lst)
    out["publications"] = [publishing.publication_view(ctx, p)
                           for p in ctx.db.scalars(select(ChannelPublication).where(ChannelPublication.listing_id == lst.id))]
    return out


class ApproveIn(BaseModel):
    revision_id: int
    acknowledged_warnings: list[str] = []
    note: str | None = None


@router.post("/listings/{listing_id}/approve")
def api_approve(listing_id: int, body: ApproveIn, ctx: ClientContext = Depends(dealer_ctx("approve"))):
    listings.approve_listing(ctx, listing_id, body.revision_id, body.acknowledged_warnings, body.note)
    return api_listing(listing_id, ctx)


class BatchIn(BaseModel):
    listing_ids: list[int]
    expected_count: int | None = None
    confirm: bool = False
    acknowledge_all_warnings: bool = False


@router.post("/listings-batch/preview")
def api_batch_preview(body: BatchIn, ctx: ClientContext = Depends(dealer_ctx("approve"))):
    return listings.batch_preview(ctx, body.listing_ids)


@router.post("/listings-batch/approve")
def api_batch_approve(body: BatchIn, ctx: ClientContext = Depends(dealer_ctx("approve"))):
    if body.expected_count is None:
        raise BadRequest("expected_count (from the preview) is required")
    return listings.batch_approve(ctx, body.listing_ids, expected_count=body.expected_count, confirm=body.confirm,
                                  acknowledge_all_warnings=body.acknowledge_all_warnings)


@router.get("/publications")
def api_publications(ctx: ClientContext = Depends(dealer_ctx())):
    return [publishing.publication_view(ctx, p) for p in ctx.db.scalars(select(ChannelPublication).order_by(ChannelPublication.id))]


@router.get("/publications/{pub_id}")
def api_publication(pub_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    return publishing.publication_view(ctx, publishing.get_publication(ctx, pub_id), with_package=True)


@router.post("/publications/{pub_id}/publish", status_code=202)
def api_publish(pub_id: int, ctx: ClientContext = Depends(dealer_ctx("publish"))):
    op = publishing.request_publish(ctx, pub_id)
    return {"operation_id": op.id, "operation": op.operation, "status": op.status}


class ManualIn(BaseModel):
    url: str = ""
    attested: bool = False
    notes: str | None = None


@router.post("/publications/{pub_id}/confirm-manual")
def api_confirm_manual(pub_id: int, body: ManualIn, ctx: ClientContext = Depends(dealer_ctx("publish"))):
    return publishing.publication_view(ctx, publishing.record_manual_publication(ctx, pub_id, url=body.url, attested=body.attested,
                                                                                  notes=body.notes))


@router.get("/reports/summary")
def api_report(days: int = 30, ctx: ClientContext = Depends(dealer_ctx())):
    return dealership_metrics(ctx, days=max(1, min(days, 365)))


@router.get("/export.csv")
def api_export(lang: str = "en", ctx: ClientContext = Depends(dealer_ctx())):
    return Response(export_listings_csv(ctx, lang), media_type="text/csv")


# --- Phase 3: customer conversations and appointments --------------------------------------------------------


def conversation_json(ctx: ClientContext, conv, *, full: bool = False) -> dict[str, Any]:
    from ..models import ConversationMessage, ResponseDraft

    out: dict[str, Any] = {
        "id": conv.id, "channel": conv.channel, "status": conv.status, "language": conv.preferred_language,
        "simulated": conv.simulated, "vehicle_id": conv.vehicle_id, "assigned_user_id": conv.assigned_user_id,
        "opted_out": conv.opted_out, "human_takeover": conv.human_takeover, "escalation_status": conv.escalation_status,
        "appointment_status": conv.appointment_status, "next_action": conv.next_action,
        "last_inbound_at": conv.last_inbound_at.isoformat() if conv.last_inbound_at else None,
    }
    if full:
        msgs = ctx.db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conv.id)
                              .order_by(ConversationMessage.id)).all()
        out["messages"] = [{"id": m.id, "direction": m.direction, "sender_type": m.sender_type, "body": m.body,
                            "delivery_status": m.delivery_status, "created_at": m.created_at.isoformat()} for m in msgs]
        draft = ctx.db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id,
                                                          ResponseDraft.status == "PENDING_REVIEW").order_by(ResponseDraft.id.desc()))
        out["pending_draft"] = None if draft is None else {
            "id": draft.id, "body": draft.body, "language": draft.language, "confidence": draft.confidence,
            "escalation_reasons": draft.escalation_reasons, "unresolved": draft.unresolved,
            "decision_reasons": draft.decision_reasons}
        out["summary"] = conv.summary
    return out


@router.get("/conversations")
def api_conversations(status: str | None = None, ctx: ClientContext = Depends(dealer_ctx())):
    from ..models import Conversation

    stmt = select(Conversation)
    if status:
        stmt = stmt.where(Conversation.status == status)
    return {"items": [conversation_json(ctx, c) for c in ctx.db.scalars(stmt.order_by(Conversation.id.desc()).limit(500))]}


@router.get("/conversations/{conv_id}")
def api_conversation(conv_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    from ..models import Conversation

    conv = ctx.db.get(Conversation, conv_id)
    if conv is None or conv.client_id != ctx.client_id:
        raise NotFound("Conversation not found")
    return conversation_json(ctx, conv, full=True)


class ReplyIn(BaseModel):
    body: str
    draft_id: int | None = None
    acknowledge_warnings: bool = False


@router.post("/conversations/{conv_id}/reply", status_code=202)
def api_reply(conv_id: int, body: ReplyIn, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    """Queue a staff reply (SIMULATED delivery in this build). Same safety check as the page: unverified content is
    refused unless ``acknowledge_warnings`` is true (recorded in the audit log)."""
    from ..models import Conversation, ResponseDraft
    from ..services.conversations.channels import SENDABLE_CHANNELS
    from ..services.conversations.common import record
    from ..services.conversations.outbound import queue_outbound, record_manual_external
    from ..services.conversations.summaries import build_summary
    from .routes_conversations import staff_message_warnings

    conv = ctx.db.get(Conversation, conv_id)
    if conv is None or conv.client_id != ctx.client_id:
        raise NotFound("Conversation not found")
    text = body.body.strip()
    if not text:
        raise BadRequest("Message is empty")
    if conv.opted_out:
        raise BadRequest("The customer opted out; messages cannot be sent from the platform")
    draft = ctx.db.get(ResponseDraft, body.draft_id) if body.draft_id else None
    if body.draft_id and (draft is None or draft.client_id != ctx.client_id or draft.conversation_id != conv.id):
        raise NotFound("Draft not found")
    warnings = staff_message_warnings(ctx.db, conv, text, draft)
    if warnings and not body.acknowledge_warnings:
        raise BadRequest("Message contains content not backed by verified data", {"warnings": warnings})
    if warnings:
        record(ctx.db, "message.staff_override", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="conversation",
               entity_id=conv.id, warnings=warnings)
    if draft:
        draft.reviewed_by_id = ctx.actor_id
    if conv.channel not in SENDABLE_CHANNELS:
        msg = record_manual_external(ctx.db, conv, text, user_id=ctx.actor_id, draft=draft)
    else:
        msg = queue_outbound(ctx.db, conv, text, sender_type="STAFF", user_id=ctx.actor_id, draft=draft)
        if draft:
            draft.status = "SENT"
    build_summary(ctx.db, conv)
    return {"message_id": msg.id, "delivery_status": msg.delivery_status}


@router.get("/appointments")
def api_appointments(ctx: ClientContext = Depends(dealer_ctx())):
    from ..models import Appointment

    rows = ctx.db.scalars(select(Appointment).order_by(Appointment.requested_start.desc()).limit(500)).all()
    return {"items": [{"id": a.id, "conversation_id": a.conversation_id, "vehicle_id": a.vehicle_id, "status": a.status,
                       "requested_start_utc": a.requested_start.isoformat(),
                       "confirmed_start_utc": a.confirmed_start.isoformat() if a.confirmed_start else None,
                       "availability_checked": a.availability_checked} for a in rows]}


@router.post("/appointments/{appt_id}/confirm")
def api_confirm_appointment(appt_id: int, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    from ..models import Appointment
    from ..services.conversations import appointments as appt_svc

    appt = ctx.db.get(Appointment, appt_id)
    if appt is None or appt.client_id != ctx.client_id:
        raise NotFound("Appointment not found")
    try:
        appt_svc.confirm(ctx.db, appt, user_id=ctx.actor_id)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return {"id": appt.id, "status": appt.status}

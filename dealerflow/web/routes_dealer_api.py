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

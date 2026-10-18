from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.api.deps import client_ctx
from app.config import settings
from app.core.audit import audit_ctx
from app.core.crypto import encrypt_secret
from app.core.errors import BadRequest, NotFound
from app.core.tenancy import ClientContext
from app.core.time import utcnow
from app.inventory.feeds import FEED_TYPES, validate_feed_config
from app.inventory.importer import get_source, run_import, upsert_manual_vehicle
from app.inventory.normalize import ALIASES, CANONICAL_FIELDS
from app.inventory.parsers import parse_file
from app.models import ImportRawRecord, ImportRun, InventorySource, Listing, Vehicle, VehicleChange, VehiclePhoto
from app.models.inventory import SOURCE_TYPES
from app.sync.jobs import enqueue_sync_now, sync_source_schedule
from app.sync.reactions import react_to_vehicle_events

router = APIRouter(prefix="/api/clients/{client_id}", tags=["inventory"])


def _clean_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Secrets belong in the encrypted `secret` field, never in plain config JSON."""
    return {k: v for k, v in (cfg or {}).items() if "token" not in k.lower() and "secret" not in k.lower() and "password" not in k.lower()}


def source_view(s: InventorySource) -> dict:
    return {"id": s.id, "name": s.name, "source_type": s.source_type, "config": s.config, "has_secret": bool(s.secret_ciphertext),
            "full_snapshot": s.full_snapshot, "schedule_minutes": s.schedule_minutes, "stale_after_hours": s.stale_after_hours,
            "enabled": s.enabled, "authorization_note": s.authorization_note, "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None,
            "last_status": s.last_status, "last_run_id": s.last_run_id}


class SourceIn(BaseModel):
    name: str
    source_type: str
    config: dict[str, Any] = {}
    secret: str | None = None
    full_snapshot: bool = True
    schedule_minutes: int | None = None
    stale_after_hours: int = 72
    enabled: bool = True
    authorization_note: str | None = None


@router.get("/inventory/schema")
def inventory_schema(ctx: ClientContext = Depends(client_ctx())):
    return {"canonical_fields": CANONICAL_FIELDS, "header_aliases": ALIASES, "source_types": SOURCE_TYPES}


@router.get("/sources")
def list_sources(ctx: ClientContext = Depends(client_ctx())):
    return [source_view(s) for s in ctx.db.scalars(select(InventorySource).order_by(InventorySource.created_at))]


@router.post("/sources", status_code=201)
def create_source(body: SourceIn, ctx: ClientContext = Depends(client_ctx("import"))):
    if body.source_type not in SOURCE_TYPES:
        raise BadRequest(f"source_type must be one of {', '.join(SOURCE_TYPES)}")
    validate_feed_config(body.source_type, body.config, body.authorization_note)
    if body.schedule_minutes is not None and body.schedule_minutes < 15:
        raise BadRequest("schedule_minutes must be at least 15")
    src = InventorySource(client_id=ctx.client_id, name=body.name, source_type=body.source_type, config=_clean_config(body.config),
                          full_snapshot=body.full_snapshot, schedule_minutes=body.schedule_minutes,
                          stale_after_hours=body.stale_after_hours, enabled=body.enabled, authorization_note=body.authorization_note)
    if body.secret:
        src.secret_ciphertext = encrypt_secret(body.secret)
    ctx.db.add(src)
    ctx.db.flush()
    sync_source_schedule(ctx.db, src)
    audit_ctx(ctx, "inventory_source.created", "inventory_source", src.id, source_type=src.source_type)
    return source_view(src)


@router.patch("/sources/{source_id}")
def update_source(source_id: str, body: dict[str, Any], ctx: ClientContext = Depends(client_ctx("import"))):
    src = get_source(ctx, source_id)
    allowed = {"name", "config", "full_snapshot", "schedule_minutes", "stale_after_hours", "enabled", "authorization_note"}
    for k, v in body.items():
        if k == "secret" and v:
            src.secret_ciphertext = encrypt_secret(v)
        elif k in allowed:
            setattr(src, k, _clean_config(v) if k == "config" else v)
    validate_feed_config(src.source_type, src.config or {}, src.authorization_note)
    sync_source_schedule(ctx.db, src)
    audit_ctx(ctx, "inventory_source.updated", "inventory_source", src.id, fields=sorted(body.keys()))
    return source_view(src)


@router.post("/sources/{source_id}/sync", status_code=202)
def sync_now(source_id: str, ctx: ClientContext = Depends(client_ctx("import"))):
    src = get_source(ctx, source_id)
    if src.source_type not in FEED_TYPES or not (src.config or {}).get("url"):
        raise BadRequest("Only feed sources with a URL can be synced; upload a file for this source instead")
    job = enqueue_sync_now(ctx.db, src)
    return {"job_id": job.id, "status": job.status}


@router.post("/imports/upload", status_code=201)
async def upload_inventory(file: UploadFile = File(...), source_id: str | None = Form(None), column_map: str | None = Form(None),
                           source_name: str | None = Form(None), ctx: ClientContext = Depends(client_ctx("import"))):
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise BadRequest(f"File exceeds {settings.max_upload_bytes // (1024 * 1024)} MB")
    kind, headers, rows = parse_file(file.filename or "upload.csv", data, file.content_type)
    if source_id:
        src = get_source(ctx, source_id)
    else:
        stype = "spreadsheet_upload" if kind == "xlsx" else "csv_upload"
        src = ctx.db.scalar(select(InventorySource).where(InventorySource.source_type == stype,
                                                          InventorySource.name == (source_name or f"{stype.replace('_', ' ').title()}")))
        if not src:
            src = InventorySource(client_id=ctx.client_id, name=source_name or stype.replace("_", " ").title(), source_type=stype,
                                  authorization_note="Uploaded by an authorised dealership/agency user")
            ctx.db.add(src)
            ctx.db.flush()
    cmap = None
    if column_map:
        try:
            cmap = json.loads(column_map)
        except json.JSONDecodeError as exc:
            raise BadRequest("column_map must be JSON") from exc
    run = run_import(ctx, src, headers, rows, trigger="upload", file_name=file.filename, file_bytes=data, column_map=cmap)
    return run_view(run)


def run_view(r: ImportRun) -> dict:
    return {"id": r.id, "source_id": r.source_id, "trigger": r.trigger, "status": r.status, "file_name": r.file_name,
            "counts": r.counts, "error": r.error, "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


@router.get("/imports")
def list_imports(limit: int = 50, ctx: ClientContext = Depends(client_ctx())):
    return [run_view(r) for r in ctx.db.scalars(select(ImportRun).order_by(ImportRun.started_at.desc()).limit(min(limit, 200)))]


@router.get("/imports/{run_id}")
def get_import(run_id: str, ctx: ClientContext = Depends(client_ctx())):
    r = ctx.db.get(ImportRun, run_id)
    if not r or r.client_id != ctx.client_id:
        raise NotFound("Import not found")
    recs = ctx.db.scalars(select(ImportRawRecord).where(ImportRawRecord.run_id == r.id).order_by(ImportRawRecord.row_number)).all()
    return {**run_view(r), "records": [{"row": x.row_number, "external_id": x.external_id, "outcome": x.outcome, "issues": x.issues,
                                        "vehicle_id": x.vehicle_id, "raw": x.raw} for x in recs]}


def vehicle_view(ctx: ClientContext, v: Vehicle, full: bool = False) -> dict:
    lst = ctx.db.scalar(select(Listing).where(Listing.vehicle_id == v.id))
    out = {"id": v.id, "external_id": v.external_id, "source_id": v.source_id, "vin": v.vin, "stock_number": v.stock_number,
           "year": v.year, "make": v.make, "model": v.model, "trim": v.trim, "body_style": v.body_style, "mileage": v.mileage,
           "price": v.price_cents / 100 if v.price_cents is not None else None, "condition": v.condition,
           "exterior_color": v.exterior_color, "interior_color": v.interior_color, "transmission": v.transmission,
           "drivetrain": v.drivetrain, "fuel_type": v.fuel_type, "engine": v.engine, "features": v.features,
           "location": {"name": v.location_name, "address": v.location_address, "city": v.location_city,
                        "state": v.location_state, "postal": v.location_postal},
           "title_status": v.title_status, "owner_count": v.owner_count,
           "inventory_status": v.inventory_status, "validation_status": v.validation_status, "validation_issues": v.validation_issues,
           "stale": v.stale, "version": v.version, "last_synced_at": v.last_synced_at.isoformat(),
           "listing": {"id": lst.id, "status": lst.status, "content_outdated": lst.content_outdated} if lst else None}
    photos = ctx.db.scalars(select(VehiclePhoto).where(VehiclePhoto.vehicle_id == v.id).order_by(VehiclePhoto.position)).all()
    out["photos"] = [p.url for p in photos] if full else [p.url for p in photos[:1]]
    out["photo_count"] = len(photos)
    if full:
        out["description"] = v.description
        out["changes"] = [{"type": c.change_type, "changes": c.changes, "run_id": c.run_id, "at": c.created_at.isoformat()}
                          for c in ctx.db.scalars(select(VehicleChange).where(VehicleChange.vehicle_id == v.id)
                                                  .order_by(VehicleChange.created_at.desc()).limit(100))]
    return out


@router.get("/vehicles")
def list_vehicles(status: str | None = None, validation: str | None = None, stale: bool | None = None,
                  q: str | None = Query(None, max_length=100), listing_status: str | None = None,
                  limit: int = 100, offset: int = 0, ctx: ClientContext = Depends(client_ctx())):
    stmt = select(Vehicle)
    if status:
        stmt = stmt.where(Vehicle.inventory_status == status)
    if validation:
        stmt = stmt.where(Vehicle.validation_status == validation)
    if stale is not None:
        stmt = stmt.where(Vehicle.stale.is_(stale))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Vehicle.make.ilike(like), Vehicle.model.ilike(like), Vehicle.vin.ilike(like),
                              Vehicle.stock_number.ilike(like), Vehicle.external_id.ilike(like)))
    if listing_status:
        stmt = stmt.join(Listing, Listing.vehicle_id == Vehicle.id).where(Listing.status == listing_status)
    total = ctx.db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = ctx.db.scalars(stmt.order_by(Vehicle.updated_at.desc()).limit(min(limit, 500)).offset(offset)).all()
    return {"total": total, "items": [vehicle_view(ctx, v) for v in rows]}


def _get_vehicle(ctx: ClientContext, vehicle_id: str) -> Vehicle:
    v = ctx.db.get(Vehicle, vehicle_id)
    if not v or v.client_id != ctx.client_id:
        raise NotFound("Vehicle not found")
    return v


@router.get("/vehicles/{vehicle_id}")
def get_vehicle(vehicle_id: str, ctx: ClientContext = Depends(client_ctx())):
    return vehicle_view(ctx, _get_vehicle(ctx, vehicle_id), full=True)


@router.post("/vehicles", status_code=201)
def create_vehicle(body: dict[str, Any], ctx: ClientContext = Depends(client_ctx("edit_inventory"))):
    v = upsert_manual_vehicle(ctx, body)
    return vehicle_view(ctx, v, full=True)


@router.patch("/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id: str, body: dict[str, Any], ctx: ClientContext = Depends(client_ctx("edit_inventory"))):
    v = upsert_manual_vehicle(ctx, body, vehicle_id=vehicle_id)
    return vehicle_view(ctx, v, full=True)


class StatusIn(BaseModel):
    status: str
    note: str | None = None


@router.post("/vehicles/{vehicle_id}/status")
def set_vehicle_status(vehicle_id: str, body: StatusIn, ctx: ClientContext = Depends(client_ctx("edit_inventory"))):
    """Record a dealer-reported status change (e.g. sold) between syncs."""
    if body.status not in ("available", "pending", "sold", "removed"):
        raise BadRequest("Invalid status")
    v = _get_vehicle(ctx, vehicle_id)
    before = v.inventory_status
    if before == body.status:
        return vehicle_view(ctx, v, full=True)
    v.inventory_status = body.status
    v.version += 1
    now = utcnow()
    if body.status == "sold":
        v.sold_at = now
    if body.status == "removed":
        v.removed_at = now
    ctype = "sold" if body.status == "sold" else "removed" if body.status == "removed" else "status_changed"
    ctx.db.add(VehicleChange(client_id=ctx.client_id, vehicle_id=v.id, change_type=ctype,
                             changes={"before": before, "after": body.status, "note": body.note, "by": ctx.actor.id}))
    audit_ctx(ctx, "vehicle.status_set", "vehicle", v.id, before=before, after=body.status, note=body.note)
    ctx.db.flush()
    events = [ctype] + (["reappeared"] if before in ("sold", "removed") and body.status == "available" else [])
    react_to_vehicle_events(ctx, [(v.id, events)])
    return vehicle_view(ctx, v, full=True)


@router.post("/vehicles/{vehicle_id}/confirm-availability")
def confirm_availability(vehicle_id: str, ctx: ClientContext = Depends(client_ctx("edit_inventory"))):
    v = _get_vehicle(ctx, vehicle_id)
    if v.inventory_status not in ("available", "pending"):
        raise BadRequest("Only available or pending vehicles can be confirmed")
    v.stale, v.last_seen_at = False, utcnow()
    audit_ctx(ctx, "vehicle.availability_confirmed", "vehicle", v.id)
    return vehicle_view(ctx, v, full=True)

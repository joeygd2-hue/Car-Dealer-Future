"""Phase 2 dealership workspace pages: inventory, listings review/approval, publishing, imports, reports, settings.

Every page is scoped to one dealership client via ``dealer_ctx`` (server-side authorisation and a tenant-bound
session). Staff open any client from /dealers; dealership users only ever see their own client.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Iterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..db import get_db, utcnow
from ..integrations.listing_channels import CHANNELS
from ..models import Prospect, User
from ..models_dealer import (
    CLIENT_ROLES, SOURCE_TYPES, ChannelPublication, Client, ImportRawRecord, ImportRun, InventorySource, Listing,
    ListingReview, ListingRevision, Vehicle, VehicleChange, VehiclePhoto,
)
from ..security import hash_password, role_at_least
from ..services import dealer_jobs
from ..services.dealer_errors import BadRequest, NotFound
from ..services.dealer_reports import dealership_metrics
from ..services.inventory.feeds import FEED_TYPES, validate_feed_config
from ..services.inventory.importer import get_source, run_import, upsert_manual_vehicle
from ..services.inventory.parsers import parse_file
from ..services.inventory.reactions import react_to_vehicle_events
from ..services.listings import service as listings
from ..services.listings.dealer_settings import get_settings as get_listing_settings
from ..services.listings.dealer_settings import update_settings
from ..services.publishing import service as publishing
from ..tenancy import ClientContext, accessible_clients, authorize_client
from .deps import check_csrf, current_user, flash, render

router = APIRouter(dependencies=[Depends(check_csrf)])

LABELS = {
    "imported": "Imported", "needs_validation": "Needs validation", "draft_generated": "Draft (needs fixes)",
    "awaiting_approval": "Awaiting approval", "approved": "Approved", "rejected": "Rejected", "sold_withdrawn": "Sold / withdrawn",
    "ready_to_publish": "Ready to publish", "submitted": "Submitted", "published_confirmed": "Published (confirmed)",
    "manual_required": "Manual publication required", "failed": "Failed", "withdrawal_required": "Withdrawal required",
    "withdrawn": "Withdrawn",
}


def dealer_ctx(permission: str = "view"):
    def dep(client_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> Iterator[ClientContext]:
        ctx = authorize_client(db, user, client_id, permission)
        request.state.dealer = ctx
        try:
            yield ctx
            ctx.db.commit()
        except Exception:
            ctx.db.rollback()
            raise
        finally:
            ctx.close()
    return dep


def page(request: Request, ctx: ClientContext, name: str, **kw):
    return render(request, name, ctx.user, ctx=ctx, client=ctx.client, labels=LABELS, **kw)


def go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --- client directory ------------------------------------------------------------------------------

@router.get("/dealers")
def dealers_index(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.client_id is not None:
        return go(f"/dealers/{user.client_id}")
    clients = accessible_clients(db, user)
    counts = dict(db.execute(select(Vehicle.client_id, func.count()).where(Vehicle.inventory_status.in_(("available", "pending")))
                             .group_by(Vehicle.client_id)).all())
    pending = dict(db.execute(select(Listing.client_id, func.count()).where(Listing.status == "awaiting_approval")
                              .group_by(Listing.client_id)).all())
    won = db.scalars(select(Prospect).where(Prospect.stage == "won").order_by(Prospect.name)).all()
    return render(request, "dealers.html", user, clients=clients, counts=counts, pending=pending, won_prospects=won)


@router.post("/dealers")
def create_client(request: Request, name: str = Form(""), prospect_id: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    if user.client_id is not None or not role_at_least(user.role, "manager"):
        raise HTTPException(403, "Requires manager role")
    prospect = db.get(Prospect, int(prospect_id)) if prospect_id.strip().isdigit() else None
    name = (name or (prospect.name if prospect else "")).strip()
    if not name:
        flash(request, "Name is required", "error")
        return go("/dealers")
    if prospect and db.scalar(select(Client).where(Client.prospect_id == prospect.id)):
        flash(request, "That prospect is already a client", "error")
        return go("/dealers")
    from ..services import tenancy as client_tenancy

    c = client_tenancy.create_client(db, name=name, actor=user, prospect=prospect, simulated=bool(prospect and prospect.simulated))
    flash(request, f"Created dealership workspace for {name}")
    return go(f"/dealers/{c.id}")


# --- inventory -------------------------------------------------------------------------------------

@router.get("/dealers/{client_id}")
def inventory(request: Request, q: str = "", status: str = "", validation: str = "", stale: str = "",
              ctx: ClientContext = Depends(dealer_ctx())):
    stmt = select(Vehicle)
    if status:
        stmt = stmt.where(Vehicle.inventory_status == status)
    if validation:
        stmt = stmt.where(Vehicle.validation_status == validation)
    if stale == "1":
        stmt = stmt.where(Vehicle.stale.is_(True))
    if q.strip():
        like = f"%{q.strip()[:100]}%"
        stmt = stmt.where(or_(Vehicle.make.ilike(like), Vehicle.model.ilike(like), Vehicle.vin.ilike(like),
                              Vehicle.stock_number.ilike(like), Vehicle.external_id.ilike(like)))
    vehicles = ctx.db.scalars(stmt.order_by(Vehicle.updated_at.desc()).limit(500)).all()
    listing_by_vehicle = {lst.vehicle_id: lst for lst in ctx.db.scalars(select(Listing))}
    first_photo = {}
    for p in ctx.db.scalars(select(VehiclePhoto).where(VehiclePhoto.position == 0)):
        first_photo[p.vehicle_id] = p.url
    return page(request, ctx, "dealer_inventory.html", vehicles=vehicles, listings=listing_by_vehicle, photos=first_photo,
                f={"q": q, "status": status, "validation": validation, "stale": stale})


def _vehicle(ctx: ClientContext, vehicle_id: int) -> Vehicle:
    v = ctx.db.get(Vehicle, vehicle_id)
    if not v or v.client_id != ctx.client_id:
        raise NotFound("Vehicle not found")
    return v


@router.get("/dealers/{client_id}/vehicles/new")
def vehicle_new(request: Request, ctx: ClientContext = Depends(dealer_ctx("edit"))):
    return page(request, ctx, "dealer_vehicle_new.html")


MANUAL_FIELDS = ("year", "make", "model", "trim", "body_style", "mileage", "price", "vin", "stock_number", "condition",
                 "exterior_color", "interior_color", "transmission", "drivetrain", "fuel_type", "engine", "features", "photos",
                 "location_address", "location_city", "location_state", "location_postal", "title_status", "owner_count")


@router.post("/dealers/{client_id}/vehicles/new")
async def vehicle_create(request: Request, ctx: ClientContext = Depends(dealer_ctx("edit"))):
    form = await request.form()
    data = {k: str(form.get(k)).strip() for k in MANUAL_FIELDS if str(form.get(k) or "").strip()}
    v = upsert_manual_vehicle(ctx, data)
    flash(request, "Vehicle added. A listing draft is generated in the background.")
    return go(f"/dealers/{ctx.client_id}/vehicles/{v.id}")


@router.get("/dealers/{client_id}/vehicles/{vehicle_id}")
def vehicle_detail(request: Request, vehicle_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    v = _vehicle(ctx, vehicle_id)
    photos = [p.url for p in ctx.db.scalars(select(VehiclePhoto).where(VehiclePhoto.vehicle_id == v.id).order_by(VehiclePhoto.position))]
    changes = ctx.db.scalars(select(VehicleChange).where(VehicleChange.vehicle_id == v.id).order_by(VehicleChange.id.desc()).limit(100)).all()
    lst = ctx.db.scalar(select(Listing).where(Listing.vehicle_id == v.id))
    source = ctx.db.get(InventorySource, v.source_id)
    return page(request, ctx, "dealer_vehicle.html", v=v, photos=photos, changes=changes, listing=lst, source=source)


@router.post("/dealers/{client_id}/vehicles/{vehicle_id}/status")
def vehicle_status(request: Request, vehicle_id: int, status: str = Form(...), note: str = Form(""),
                   ctx: ClientContext = Depends(dealer_ctx("edit"))):
    if status not in ("available", "pending", "sold", "removed"):
        raise BadRequest("Invalid status")
    v = _vehicle(ctx, vehicle_id)
    before = v.inventory_status
    if before != status:
        v.inventory_status, v.version = status, v.version + 1
        if status == "sold":
            v.sold_at = utcnow()
        if status == "removed":
            v.removed_at = utcnow()
        ctype = "sold" if status == "sold" else "removed" if status == "removed" else "status_changed"
        ctx.db.add(VehicleChange(client_id=ctx.client_id, vehicle_id=v.id, change_type=ctype,
                                 changes={"before": before, "after": status, "note": note, "by": ctx.actor_label}))
        audit(ctx.db, "vehicle.status_set", actor_id=ctx.actor_id, entity_type="vehicle", entity_id=v.id,
              data={"client_id": ctx.client_id, "before": before, "after": status, "note": note})
        ctx.db.flush()
        react_to_vehicle_events(ctx, [(v.id, [ctype] + (["reappeared"] if before in ("sold", "removed") and status == "available" else []))])
        flash(request, f"Marked {status}." + (" Live listings are flagged for withdrawal." if status in ("sold", "removed") else ""))
    return go(f"/dealers/{ctx.client_id}/vehicles/{v.id}")


@router.post("/dealers/{client_id}/vehicles/{vehicle_id}/confirm")
def vehicle_confirm(request: Request, vehicle_id: int, ctx: ClientContext = Depends(dealer_ctx("edit"))):
    v = _vehicle(ctx, vehicle_id)
    if v.inventory_status not in ("available", "pending"):
        raise BadRequest("Only available or pending vehicles can be confirmed")
    v.stale, v.last_seen_at = False, utcnow()
    audit(ctx.db, "vehicle.availability_confirmed", actor_id=ctx.actor_id, entity_type="vehicle", entity_id=v.id,
          data={"client_id": ctx.client_id})
    flash(request, "Availability confirmed")
    return go(f"/dealers/{ctx.client_id}/vehicles/{v.id}")


# --- listings --------------------------------------------------------------------------------------

@router.get("/dealers/{client_id}/listings")
def listings_index(request: Request, status: str = "", ctx: ClientContext = Depends(dealer_ctx())):
    stmt = select(Listing, Vehicle).join(Vehicle, (Vehicle.id == Listing.vehicle_id) & (Vehicle.client_id == Listing.client_id))
    if status:
        stmt = stmt.where(Listing.status == status)
    rows = []
    pubs: dict[int, list[ChannelPublication]] = {}
    for p in ctx.db.scalars(select(ChannelPublication)):
        pubs.setdefault(p.listing_id, []).append(p)
    for lst, v in ctx.db.execute(stmt.order_by(Listing.updated_at.desc()).limit(500)).all():
        rev = listings.current_revision(ctx, lst)
        rows.append({"l": lst, "v": v, "rev": rev, "pubs": pubs.get(lst.id, [])})
    return page(request, ctx, "dealer_listings.html", rows=rows, status=status, statuses=list(LABELS)[:7])


@router.post("/dealers/{client_id}/listings/batch-preview")
async def batch_preview(request: Request, ctx: ClientContext = Depends(dealer_ctx("approve"))):
    form = await request.form()
    ids = [int(x) for x in form.getlist("listing_id") if str(x).isdigit()]
    if not ids:
        flash(request, "Select listings first", "warn")
        return go(f"/dealers/{ctx.client_id}/listings")
    preview = listings.batch_preview(ctx, ids)
    return page(request, ctx, "dealer_batch.html", preview=preview, ids=ids)


@router.post("/dealers/{client_id}/listings/batch-approve")
async def batch_approve(request: Request, ctx: ClientContext = Depends(dealer_ctx("approve"))):
    form = await request.form()
    ids = [int(x) for x in form.getlist("listing_id") if str(x).isdigit()]
    res = listings.batch_approve(ctx, ids, expected_count=int(form.get("expected_count") or -1), confirm=form.get("confirm") == "yes",
                                 acknowledge_all_warnings=form.get("acknowledge_all_warnings") == "yes")
    flash(request, f"Approved {len(res['approved'])} listing(s); skipped {len(res['skipped'])}.")
    return go(f"/dealers/{ctx.client_id}/listings")


@router.get("/dealers/{client_id}/listings/{listing_id}")
def listing_detail(request: Request, listing_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    lst = listings.get_listing(ctx, listing_id)
    view = listings.listing_view(ctx, lst)
    v = _vehicle(ctx, lst.vehicle_id)
    pubs = [publishing.publication_view(ctx, p) for p in ctx.db.scalars(select(ChannelPublication).where(ChannelPublication.listing_id == lst.id))]
    revisions = ctx.db.scalars(select(ListingRevision).where(ListingRevision.listing_id == lst.id)
                               .order_by(ListingRevision.revision_number.desc())).all()
    reviews = ctx.db.scalars(select(ListingReview).where(ListingReview.listing_id == lst.id).order_by(ListingReview.id.desc())).all()
    s, _ = get_listing_settings(ctx)
    can_approve = ctx.can("approve") and not (ctx.is_staff and not s.review.allow_agency_approval)
    hard_blockers = [b for b in view.get("approval_blockers", []) if "acknowledged" not in b]
    return page(request, ctx, "dealer_listing.html", l=view, lst=lst, v=v, pubs=pubs, revisions=revisions, reviews=reviews,
                can_approve=can_approve, agency_blocked=ctx.is_staff and not s.review.allow_agency_approval,
                hard_blockers=hard_blockers)


@router.post("/dealers/{client_id}/listings/{listing_id}/generate")
def listing_generate(request: Request, listing_id: int, ctx: ClientContext = Depends(dealer_ctx("edit"))):
    rev = listings.generate_listing(ctx, listing_id)
    flash(request, f"Draft regenerated (revision {rev.revision_number}).")
    return go(f"/dealers/{ctx.client_id}/listings/{listing_id}")


@router.post("/dealers/{client_id}/listings/{listing_id}/edit")
async def listing_edit(request: Request, listing_id: int, ctx: ClientContext = Depends(dealer_ctx("edit"))):
    form = await request.form()
    lst = listings.get_listing(ctx, listing_id)
    rev = listings.current_revision(ctx, lst)
    if not rev:
        raise BadRequest("Generate a draft before editing")
    changes: dict[str, dict] = {}
    for lang in ("en", "es"):
        for field in ("title", "description"):
            val = form.get(f"{lang}_{field}")
            if val is not None and lang in rev.content and str(val).replace("\r\n", "\n") != rev.content[lang][field]:
                changes.setdefault(lang, {})[field] = str(val).replace("\r\n", "\n")
    if not changes:
        flash(request, "No changes to save", "warn")
    else:
        new = listings.edit_listing(ctx, listing_id, changes, note=str(form.get("note") or "") or None)
        flash(request, f"Saved as revision {new.revision_number} and re-checked.")
    return go(f"/dealers/{ctx.client_id}/listings/{listing_id}")


@router.post("/dealers/{client_id}/listings/{listing_id}/approve")
async def listing_approve(request: Request, listing_id: int, ctx: ClientContext = Depends(dealer_ctx("approve"))):
    form = await request.form()
    listings.approve_listing(ctx, listing_id, int(form.get("revision_id") or 0), [str(x) for x in form.getlist("ack")],
                             note=str(form.get("note") or "") or None)
    flash(request, "Approved. Channel packages are ready on this page.")
    return go(f"/dealers/{ctx.client_id}/listings/{listing_id}")


@router.post("/dealers/{client_id}/listings/{listing_id}/reject")
def listing_reject(request: Request, listing_id: int, note: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("approve"))):
    listings.reject_listing(ctx, listing_id, note)
    flash(request, "Rejected")
    return go(f"/dealers/{ctx.client_id}/listings/{listing_id}")


@router.post("/dealers/{client_id}/listings/{listing_id}/regenerate")
def listing_regenerate(request: Request, listing_id: int, note: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("edit"))):
    listings.request_regeneration(ctx, listing_id, note or None)
    flash(request, "Regeneration requested and a new draft created.")
    return go(f"/dealers/{ctx.client_id}/listings/{listing_id}")


# --- publishing ------------------------------------------------------------------------------------

@router.get("/dealers/{client_id}/publishing")
def publishing_index(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    pubs = [publishing.publication_view(ctx, p) for p in ctx.db.scalars(select(ChannelPublication).order_by(ChannelPublication.updated_at.desc()))]
    attention = [p for p in pubs if p["needs_update"] or p["status"] in ("withdrawal_required", "failed", "manual_required")]
    connections = {k: publishing.get_connection(ctx, k) for k in CHANNELS}
    return page(request, ctx, "dealer_publishing.html", pubs=pubs, attention=attention, channels=CHANNELS, connections=connections)


@router.post("/dealers/{client_id}/channels/{channel}")
async def channel_connection(request: Request, channel: str, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    form = await request.form()
    config = {k: str(form.get(k)).strip() for k in ("catalog_id", "vdp_url_template", "language") if str(form.get(k) or "").strip()}
    live = form.get("live_publishing") == "yes"
    if live and form.get("confirm_live") != f"AUTHORIZE {ctx.client.name}":
        raise BadRequest(f'Type "AUTHORIZE {ctx.client.name}" to authorise live publishing')
    publishing.upsert_connection(ctx, channel, config=config, token_env=str(form.get("token_env") or "").strip(), live_publishing=live)
    flash(request, "Channel connection saved")
    return go(f"/dealers/{ctx.client_id}/publishing")


@router.get("/dealers/{client_id}/publications/{pub_id}")
def publication_detail(request: Request, pub_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    p = publishing.publication_view(ctx, publishing.get_publication(ctx, pub_id), with_package=True)
    return page(request, ctx, "dealer_publication.html", p=p)


@router.post("/dealers/{client_id}/publications/{pub_id}/publish")
def publication_publish(request: Request, pub_id: int, ctx: ClientContext = Depends(dealer_ctx("publish"))):
    op = publishing.request_publish(ctx, pub_id)
    flash(request, "Package prepared." if op.operation == "prepare_manual" else
          "Queued. The status updates when the channel responds; it is not marked published until then.")
    return go(f"/dealers/{ctx.client_id}/publications/{pub_id}")


@router.post("/dealers/{client_id}/publications/{pub_id}/confirm-manual")
def publication_confirm(request: Request, pub_id: int, url: str = Form(""), attested: str = Form(""), notes: str = Form(""),
                        ctx: ClientContext = Depends(dealer_ctx("publish"))):
    publishing.record_manual_publication(ctx, pub_id, url=url.strip(), attested=attested == "yes", notes=notes or None)
    flash(request, "Publication recorded")
    return go(f"/dealers/{ctx.client_id}/publications/{pub_id}")


@router.post("/dealers/{client_id}/publications/{pub_id}/record-update")
def publication_updated(request: Request, pub_id: int, attested: str = Form(""), notes: str = Form(""),
                        ctx: ClientContext = Depends(dealer_ctx("publish"))):
    publishing.record_manual_update(ctx, pub_id, attested=attested == "yes", notes=notes or None)
    flash(request, "Update recorded")
    return go(f"/dealers/{ctx.client_id}/publications/{pub_id}")


@router.post("/dealers/{client_id}/publications/{pub_id}/record-removal")
def publication_removed(request: Request, pub_id: int, attested: str = Form(""), notes: str = Form(""),
                        ctx: ClientContext = Depends(dealer_ctx("publish"))):
    publishing.record_manual_removal(ctx, pub_id, attested=attested == "yes", notes=notes or None)
    flash(request, "Removal recorded")
    return go(f"/dealers/{ctx.client_id}/publications/{pub_id}")


@router.post("/dealers/{client_id}/publications/{pub_id}/retry")
def publication_retry(request: Request, pub_id: int, ctx: ClientContext = Depends(dealer_ctx("publish"))):
    publishing.retry_publication(ctx, pub_id)
    flash(request, "Retry queued")
    return go(f"/dealers/{ctx.client_id}/publications/{pub_id}")


@router.get("/dealers/{client_id}/export.csv")
def export_csv(request: Request, lang: str = "en", ctx: ClientContext = Depends(dealer_ctx())):
    return Response(export_listings_csv(ctx, lang), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="approved-listings-{lang}.csv"'})


def export_listings_csv(ctx: ClientContext, lang: str) -> str:
    if lang not in ("en", "es"):
        raise BadRequest("lang must be en or es")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["vehicle_id", "vin", "stock_number", "year", "make", "model", "trim", "mileage", "price", "title", "description",
                "photos", "status"])
    for lst, v in ctx.db.execute(select(Listing, Vehicle).join(Vehicle, (Vehicle.id == Listing.vehicle_id) & (Vehicle.client_id == Listing.client_id))
                                 .where(Listing.status == "approved", Vehicle.inventory_status.in_(("available", "pending")),
                                        Vehicle.stale.is_(False))).all():
        rev = ctx.db.get(ListingRevision, lst.approved_revision_id)
        lc = rev.content.get(lang) or {}
        w.writerow([v.id, v.vin, v.stock_number, v.year, v.make, v.model, v.trim, v.mileage,
                    v.price_cents / 100 if v.price_cents else "", lc.get("title"), lc.get("description"),
                    "|".join(rev.content.get("photos", [])), v.inventory_status])
    audit(ctx.db, "listings.exported", actor_id=ctx.actor_id, entity_type="client", entity_id=ctx.client_id,
          data={"client_id": ctx.client_id, "lang": lang})
    return buf.getvalue()


# --- imports & sources -----------------------------------------------------------------------------

@router.get("/dealers/{client_id}/imports")
def imports_index(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    runs = ctx.db.scalars(select(ImportRun).order_by(ImportRun.id.desc()).limit(100)).all()
    sources = ctx.db.scalars(select(InventorySource).order_by(InventorySource.id)).all()
    return page(request, ctx, "dealer_imports.html", runs=runs, sources=sources, source_types=SOURCE_TYPES, feed_types=FEED_TYPES)


@router.post("/dealers/{client_id}/imports")
async def imports_upload(request: Request, file: UploadFile = File(...), source_id: str = Form(""), column_map: str = Form(""),
                         ctx: ClientContext = Depends(dealer_ctx("import"))):
    run = upload_inventory(ctx, file.filename or "upload.csv", await file.read(get_settings().max_upload_bytes + 1),
                           file.content_type, source_id, column_map)
    flash(request, f"Import {run.status}: {run.counts.get('created', 0)} new, {run.counts.get('updated', 0)} updated, "
                   f"{run.counts.get('rejected', 0)} rejected", "info" if run.status == "succeeded" else "warn")
    return go(f"/dealers/{ctx.client_id}/imports/{run.id}")


def upload_inventory(ctx: ClientContext, filename: str, data: bytes, content_type: str | None, source_id: str = "",
                     column_map: str = "") -> ImportRun:
    if len(data) > get_settings().max_upload_bytes:
        raise BadRequest(f"File exceeds {get_settings().max_upload_bytes // (1024 * 1024)} MB")
    kind, headers, rows = parse_file(filename, data, content_type)
    if str(source_id).strip().isdigit():
        src = get_source(ctx, int(source_id))
    else:
        stype = "spreadsheet_upload" if kind == "xlsx" else "csv_upload"
        name = "Spreadsheet uploads" if kind == "xlsx" else "CSV uploads"
        src = ctx.db.scalar(select(InventorySource).where(InventorySource.source_type == stype, InventorySource.name == name))
        if not src:
            src = InventorySource(client_id=ctx.client_id, name=name, source_type=stype,
                                  authorization_note="Uploaded by an authorised dealership/agency user")
            ctx.db.add(src)
            ctx.db.flush()
    cmap = None
    if column_map.strip():
        try:
            cmap = json.loads(column_map)
        except json.JSONDecodeError as exc:
            raise BadRequest("Column mapping must be JSON, e.g. {\"price\": \"Internet Price\"}") from exc
    return run_import(ctx, src, headers, rows, trigger="upload", file_name=filename, file_bytes=data, column_map=cmap)


@router.post("/dealers/{client_id}/sources")
async def source_create(request: Request, ctx: ClientContext = Depends(dealer_ctx("import"))):
    form = await request.form()
    stype = str(form.get("source_type") or "")
    if stype not in SOURCE_TYPES:
        raise BadRequest("Unknown source type")
    config = {"url": str(form.get("url") or "").strip(), "format": str(form.get("format") or "csv")}
    if form.get("auth_type"):
        config["auth"] = {"type": str(form.get("auth_type"))}
    config = {k: v for k, v in config.items() if v}
    note = str(form.get("authorization_note") or "").strip() or None
    secret_env = str(form.get("secret_env") or "").strip() or None
    validate_feed_config(stype, config, note, secret_env)
    minutes = int(form.get("schedule_minutes")) if str(form.get("schedule_minutes") or "").isdigit() else None
    if minutes is not None and minutes < 15:
        raise BadRequest("Sync interval must be at least 15 minutes")
    src = InventorySource(client_id=ctx.client_id, name=str(form.get("name") or stype).strip()[:200], source_type=stype, config=config,
                          secret_env=secret_env, schedule_minutes=minutes, authorization_note=note,
                          full_snapshot=form.get("full_snapshot", "yes") == "yes")
    ctx.db.add(src)
    ctx.db.flush()
    audit(ctx.db, "inventory_source.created", actor_id=ctx.actor_id, entity_type="inventory_source", entity_id=src.id,
          data={"client_id": ctx.client_id, "source_type": stype})
    flash(request, "Source saved")
    return go(f"/dealers/{ctx.client_id}/imports")


@router.post("/dealers/{client_id}/sources/{source_id}/sync")
def source_sync(request: Request, source_id: int, ctx: ClientContext = Depends(dealer_ctx("import"))):
    src = get_source(ctx, source_id)
    if src.source_type not in FEED_TYPES or not (src.config or {}).get("url"):
        raise BadRequest("Only feed sources with a URL can be synced; upload a file for this source instead")
    dealer_jobs.sync_now(ctx.db, src)
    flash(request, "Sync queued; the worker runs it shortly.")
    return go(f"/dealers/{ctx.client_id}/imports")


@router.get("/dealers/{client_id}/imports/{run_id}")
def import_detail(request: Request, run_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    run = ctx.db.get(ImportRun, run_id)
    if not run or run.client_id != ctx.client_id:
        raise NotFound("Import not found")
    records = ctx.db.scalars(select(ImportRawRecord).where(ImportRawRecord.run_id == run.id).order_by(ImportRawRecord.row_number)).all()
    return page(request, ctx, "dealer_import.html", run=run, records=records)


# --- reports & settings ----------------------------------------------------------------------------

@router.get("/dealers/{client_id}/reports")
def reports(request: Request, days: int = 30, ctx: ClientContext = Depends(dealer_ctx())):
    return page(request, ctx, "dealer_reports.html", m=dealership_metrics(ctx, days=max(1, min(days, 365))))


@router.get("/dealers/{client_id}/settings")
def settings_page(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    s, version = get_listing_settings(ctx)
    users = ctx.db.scalars(select(User).where(User.client_id == ctx.client_id).order_by(User.email)).all()
    return page(request, ctx, "dealer_settings.html", s=s, version=version, users=users, client_roles=CLIENT_ROLES,
                can_manage=ctx.can("manage"), channel_keys=list(CHANNELS))


@router.post("/dealers/{client_id}/settings")
async def settings_save(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    f = await request.form()
    g = lambda k: str(f.get(k) or "").strip()  # noqa: E731
    split = lambda k: [x.strip() for x in g(k).split(",") if x.strip()]  # noqa: E731
    s, _ = get_listing_settings(ctx)
    disclaimers = []
    for line in g("disclaimers").splitlines():
        if line.strip():
            en, _, es = line.partition("||")
            disclaimers.append({"en": en.strip(), "es": es.strip()})
    update_settings(ctx, {
        "brand_voice": g("brand_voice") or "professional", "brand_voice_notes": g("brand_voice_notes"),
        "contact": {"dealership_name": g("dealership_name"), "phone": g("phone"), "email": g("email"), "website": g("website"),
                    "address": g("address")},
        "business_hours": {"en": g("hours_en"), "es": g("hours_es")},
        "call_to_action": {"en": g("cta_en"), "es": g("cta_es")},
        "financing_statement": {"en": g("fin_en"), "es": g("fin_es")},
        "disclaimers": disclaimers, "title_template": g("title_template") or s.title_template,
        "required_fields": split("required_fields"), "enabled_channels": [c for c in split("enabled_channels") if c in CHANNELS],
        "languages": [x for x in ("en", "es") if f.get(f"lang_{x}") == "yes"] or ["en"],
        "review": {**s.review.model_dump(), "allow_agency_approval": f.get("allow_agency_approval") == "yes",
                   "require_both_languages": f.get("require_both_languages") == "yes"},
        "show_vin": f.get("show_vin") == "yes", "use_dealer_description": f.get("use_dealer_description") == "yes",
    })
    flash(request, "Settings saved. Regenerate drafts to apply them to existing listings.")
    return go(f"/dealers/{ctx.client_id}/settings")


@router.post("/dealers/{client_id}/users")
def dealer_user_create(request: Request, email: str = Form(...), role: str = Form(...), password: str = Form(...),
                       ctx: ClientContext = Depends(dealer_ctx("manage"))):
    if role not in CLIENT_ROLES:
        raise BadRequest("Unknown dealership role")
    email = email.strip().lower()
    if ctx.db.scalar(select(User).where(User.email == email)):
        raise BadRequest("A user with that email already exists")
    try:
        pw = hash_password(password)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    # Dealership users are "client" users (confined to the workspace and portal); client_role sets their permissions.
    u = User(email=email, name=email.split("@")[0], role="client", password_hash=pw, client_id=ctx.client_id, client_role=role)
    ctx.db.add(u)
    ctx.db.flush()
    audit(ctx.db, "client_user.created", actor_id=ctx.actor_id, entity_type="user", entity_id=u.id,
          data={"client_id": ctx.client_id, "client_role": role})
    flash(request, f"Created {role.replace('_', ' ')} {email}")
    return go(f"/dealers/{ctx.client_id}/settings")

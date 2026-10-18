"""Inventory import pipeline: parse -> normalise -> validate -> upsert -> diff -> history.

Every import creates an ImportRun, preserves each raw row, records detected
changes in VehicleChange and appends to the audit log. Vehicles are matched by
(source, external_id); external_id falls back to VIN, then stock number.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.audit import audit_ctx
from app.core.errors import BadRequest, NotFound
from app.core.tenancy import ClientContext
from app.core.time import fingerprint, sha256, utcnow
from app.inventory.normalize import CANONICAL_FIELDS, build_header_map, normalize_record
from app.inventory.validate import status_from_issues, validate_vehicle
from app.models import ImportRawRecord, ImportRun, InventorySource, Vehicle, VehicleChange, VehiclePhoto

CONTENT_FIELDS = [f for f in CANONICAL_FIELDS if f not in ("photos", "external_id", "status")]
DETAIL_FIELDS = [f for f in CONTENT_FIELDS if f != "price"]
# Safeguard: never auto-remove most of a dealer's inventory because one feed came back short.
MASS_REMOVAL_FRACTION = 0.5
MASS_REMOVAL_MIN = 3


def _vehicle_to_record(v: Vehicle, photos: list[str]) -> dict[str, Any]:
    rec = {f: getattr(v, f, None) for f in CONTENT_FIELDS if f != "price"}
    rec["price"] = v.price_cents
    rec["features"] = list(v.features or [])
    rec["photos"] = photos
    return rec


def _apply_record(v: Vehicle, rec: dict[str, Any]) -> None:
    for f in CONTENT_FIELDS:
        if f == "price":
            v.price_cents = rec["price"]
        else:
            setattr(v, f, rec.get(f))


def _photo_urls(db, vehicle_id: str) -> list[str]:
    return [p.url for p in db.scalars(select(VehiclePhoto).where(VehiclePhoto.vehicle_id == vehicle_id)
                                      .order_by(VehiclePhoto.position))]


def _replace_photos(db, v: Vehicle, urls: list[str]) -> None:
    for p in db.scalars(select(VehiclePhoto).where(VehiclePhoto.vehicle_id == v.id)).all():
        db.delete(p)
    db.flush()
    for i, url in enumerate(urls):
        db.add(VehiclePhoto(client_id=v.client_id, vehicle_id=v.id, url=url, position=i))


def get_source(ctx: ClientContext, source_id: str) -> InventorySource:
    src = ctx.db.get(InventorySource, source_id)
    if not src or src.client_id != ctx.client_id:
        raise NotFound("Inventory source not found")
    return src


def ensure_manual_source(ctx: ClientContext) -> InventorySource:
    src = ctx.db.scalar(select(InventorySource).where(InventorySource.source_type == "manual"))
    if not src:
        src = InventorySource(client_id=ctx.client_id, name="Manual entry", source_type="manual", full_snapshot=False,
                              stale_after_hours=24 * 14,
                              authorization_note="Entered by dealership/agency users in the app")
        ctx.db.add(src)
        ctx.db.flush()
    return src


def run_import(ctx: ClientContext, source: InventorySource, headers: list[str], rows: list[dict[str, Any]], *,
               trigger: str, file_name: str | None = None, file_bytes: bytes | None = None,
               column_map: dict[str, str] | None = None) -> ImportRun:
    db = ctx.db
    now = utcnow()
    run = ImportRun(client_id=ctx.client_id, source_id=source.id, trigger=trigger, status="running", file_name=file_name,
                    file_sha256=sha256(file_bytes) if file_bytes else None, started_by=ctx.actor.id, started_at=now)
    db.add(run)
    db.flush()

    header_map = build_header_map(headers, column_map or (source.config or {}).get("column_map"))
    counts = {"rows": len(rows), "created": 0, "updated": 0, "unchanged": 0, "rejected": 0, "duplicate_in_file": 0,
              "removed": 0, "sold": 0, "price_changed": 0, "photos_changed": 0, "invalid": 0, "with_warnings": 0}
    events: list[tuple[str, list[str]]] = []  # (vehicle_id, change_types)
    seen_ids: set[str] = set()
    unmapped = [h for h in headers if h not in header_map]

    if not rows or not header_map:
        run.status = "failed"
        run.error = "No rows found" if not rows else "No recognisable columns; configure a column mapping"
        run.counts = {**counts, "unmapped_headers": unmapped}
        run.finished_at = utcnow()
        source.last_run_id, source.last_sync_at, source.last_status = run.id, now, run.status
        audit_ctx(ctx, "inventory.import_failed", "import_run", run.id, error=run.error)
        db.flush()
        return run

    for raw in rows:
        row_no = raw.get("__row_number", 0)
        clean_raw = {k: v for k, v in raw.items() if not k.startswith("__")}
        rec, issues = normalize_record(raw, header_map)
        ext = rec["external_id"] or rec["vin"] or rec["stock_number"]
        if not ext:
            counts["rejected"] += 1
            db.add(ImportRawRecord(client_id=ctx.client_id, run_id=run.id, row_number=row_no, raw=clean_raw,
                                   outcome="rejected", issues=issues + [{"code": "missing_identifier", "severity": "error",
                                   "field": "external_id", "message": "Row has no inventory ID, VIN or stock number"}]))
            continue
        ext = str(ext)
        if ext in seen_ids:
            counts["duplicate_in_file"] += 1
            db.add(ImportRawRecord(client_id=ctx.client_id, run_id=run.id, row_number=row_no, external_id=ext, raw=clean_raw,
                                   outcome="duplicate_in_file", issues=[{"code": "duplicate_in_file", "severity": "warning",
                                   "field": "external_id", "message": f"Vehicle {ext} appears more than once; first row was used"}]))
            continue
        seen_ids.add(ext)
        rec["external_id"] = ext
        issues = issues + validate_vehicle(rec)

        if rec["vin"]:
            dup = db.scalar(select(Vehicle).where(Vehicle.vin == rec["vin"],
                                                  ~((Vehicle.source_id == source.id) & (Vehicle.external_id == ext)),
                                                  Vehicle.inventory_status.in_(("available", "pending", "unknown"))))
            if dup:
                issues.append({"code": "duplicate_vin", "field": "vin", "severity": "warning",
                               "message": f"Same VIN as vehicle {dup.external_id} from another source; not merged automatically"})

        content_fp = fingerprint({f: rec.get(f) for f in CONTENT_FIELDS})
        photo_fp = fingerprint(rec["photos"])
        val_status = status_from_issues(issues)
        counts["invalid"] += val_status == "invalid"
        counts["with_warnings"] += val_status == "warnings"
        incoming_status = rec["status"] or "available"

        v = db.scalar(select(Vehicle).where(Vehicle.source_id == source.id, Vehicle.external_id == ext))
        change_types: list[str] = []
        if v is None:
            v = Vehicle(client_id=ctx.client_id, source_id=source.id, external_id=ext, content_fingerprint=content_fp,
                        photo_fingerprint=photo_fp, inventory_status=incoming_status, first_seen_at=now)
            _apply_record(v, rec)
            db.add(v)
            db.flush()
            _replace_photos(db, v, rec["photos"])
            change_types.append("created")
            db.add(VehicleChange(client_id=ctx.client_id, vehicle_id=v.id, run_id=run.id, change_type="created",
                                 changes={"external_id": ext}))
            outcome = "created"
            counts["created"] += 1
        else:
            before = _vehicle_to_record(v, _photo_urls(db, v.id))
            diffs = {f: {"before": before.get(f), "after": rec.get(f)} for f in CONTENT_FIELDS if before.get(f) != rec.get(f)}
            if "price" in diffs:
                change_types.append("price_changed")
                counts["price_changed"] += 1
                db.add(VehicleChange(client_id=ctx.client_id, vehicle_id=v.id, run_id=run.id, change_type="price_changed",
                                     changes={"price": diffs["price"]}))
            detail = {k: d for k, d in diffs.items() if k != "price"}
            if detail:
                change_types.append("details_changed")
                db.add(VehicleChange(client_id=ctx.client_id, vehicle_id=v.id, run_id=run.id, change_type="details_changed",
                                     changes=detail))
            if photo_fp != v.photo_fingerprint:
                change_types.append("photos_changed")
                counts["photos_changed"] += 1
                db.add(VehicleChange(client_id=ctx.client_id, vehicle_id=v.id, run_id=run.id, change_type="photos_changed",
                                     changes={"before": len(before["photos"]), "after": len(rec["photos"])}))
                _replace_photos(db, v, rec["photos"])
            if v.inventory_status in ("removed", "sold") and incoming_status == "available":
                change_types.append("reappeared")
                db.add(VehicleChange(client_id=ctx.client_id, vehicle_id=v.id, run_id=run.id, change_type="reappeared",
                                     changes={"previous_status": v.inventory_status}))
                v.removed_at = None
                v.sold_at = None
            if diffs or "photos_changed" in change_types:
                _apply_record(v, rec)
                v.content_fingerprint, v.photo_fingerprint = content_fp, photo_fp
                v.version += 1
                outcome = "updated"
                counts["updated"] += 1
            else:
                outcome = "unchanged"
                counts["unchanged"] += 1

        if incoming_status != v.inventory_status and "created" not in change_types:
            ct = "sold" if incoming_status == "sold" else "status_changed"
            db.add(VehicleChange(client_id=ctx.client_id, vehicle_id=v.id, run_id=run.id, change_type=ct,
                                 changes={"before": v.inventory_status, "after": incoming_status}))
            change_types.append(ct)
            v.inventory_status = incoming_status
            v.version += 1 if outcome == "unchanged" else 0
        if incoming_status == "sold" and not v.sold_at:
            v.sold_at = now
            counts["sold"] += 1
        v.validation_issues, v.validation_status = issues, val_status
        v.last_seen_at = v.last_synced_at = now
        if v.stale:
            v.stale = False
        db.add(ImportRawRecord(client_id=ctx.client_id, run_id=run.id, row_number=row_no, external_id=ext, raw=clean_raw,
                               outcome=outcome, vehicle_id=v.id, issues=issues))
        if change_types:
            events.append((v.id, change_types))

    accepted = counts["rows"] - counts["rejected"] - counts["duplicate_in_file"]
    run.status = "succeeded" if counts["rejected"] == 0 else ("partial" if accepted > 0 else "failed")

    # Removal detection for full-snapshot sources.
    if source.full_snapshot and run.status in ("succeeded", "partial") and accepted > 0:
        missing = db.scalars(select(Vehicle).where(Vehicle.source_id == source.id,
                                                   Vehicle.inventory_status.in_(("available", "pending", "unknown")),
                                                   Vehicle.external_id.not_in(seen_ids))).all()
        active = db.scalars(select(Vehicle.id).where(Vehicle.source_id == source.id,
                                                     Vehicle.inventory_status.in_(("available", "pending", "unknown")))).all()
        if missing and len(missing) >= MASS_REMOVAL_MIN and len(missing) > MASS_REMOVAL_FRACTION * len(active):
            counts["removal_blocked"] = len(missing)
            run.error = (f"{len(missing)} of {len(active)} active vehicles were missing from this file; automatic removal "
                         "was skipped as a safeguard. Confirm the file is complete, then re-run or remove vehicles manually.")
            run.status = "partial"
        elif run.status == "succeeded" or counts["rejected"] == 0:
            for v in missing:
                db.add(VehicleChange(client_id=ctx.client_id, vehicle_id=v.id, run_id=run.id, change_type="removed",
                                     changes={"reason": "absent_from_full_snapshot", "previous_status": v.inventory_status}))
                v.inventory_status, v.removed_at = "removed", now
                v.version += 1
                counts["removed"] += 1
                events.append((v.id, ["removed"]))

    run.counts = {**counts, "unmapped_headers": unmapped, "header_map": header_map}
    run.finished_at = utcnow()
    source.last_run_id, source.last_sync_at, source.last_status = run.id, now, run.status
    audit_ctx(ctx, "inventory.imported", "import_run", run.id, source_id=source.id, trigger=trigger,
              status=run.status, counts={k: v for k, v in counts.items()})
    db.flush()

    from app.sync.reactions import react_to_vehicle_events  # local import avoids a cycle
    react_to_vehicle_events(ctx, events)
    return run


def upsert_manual_vehicle(ctx: ClientContext, data: dict[str, Any], vehicle_id: str | None = None) -> Vehicle:
    """Manual entry/edit goes through the same normalise/validate/diff path."""
    if vehicle_id:
        v = ctx.db.get(Vehicle, vehicle_id)
        if not v or v.client_id != ctx.client_id:
            raise NotFound("Vehicle not found")
        source = ctx.db.get(InventorySource, v.source_id)
        if source.source_type != "manual":
            raise BadRequest("Vehicles from an automated source must be corrected in the source system; "
                             "the next sync would overwrite manual edits.")
        base = _vehicle_to_record(v, _photo_urls(ctx.db, v.id))
        base.update({k: v2 for k, v2 in data.items() if k in CANONICAL_FIELDS})
        base["external_id"] = v.external_id
        base["status"] = data.get("status", v.inventory_status)
        if "price" in data and data["price"] is not None:
            base["price"] = data["price"]
        else:
            base["price"] = None if base["price"] is None else f"{base['price'] / 100:.2f}"
        row = base
    else:
        source = ensure_manual_source(ctx)
        row = {k: data.get(k) for k in CANONICAL_FIELDS}
        row["external_id"] = data.get("external_id") or data.get("vin") or data.get("stock_number")
        if not row["external_id"]:
            from app.core.time import new_id
            row["external_id"] = new_id("man")
    headers = [k for k in CANONICAL_FIELDS]
    row = {k: ("|".join(row[k]) if isinstance(row.get(k), list) else row.get(k)) for k in headers}
    row["__row_number"] = 1
    run_import(ctx, source, headers, [row], trigger="manual", column_map={k: k for k in headers})
    return ctx.db.scalar(select(Vehicle).where(Vehicle.source_id == source.id, Vehicle.external_id == str(row["external_id"])))

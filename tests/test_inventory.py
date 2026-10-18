from __future__ import annotations

import io

import httpx
from openpyxl import Workbook
from sqlalchemy import select

from app.inventory import feeds
from app.inventory.vin import check_digit, vin_problems
from app.models import ImportRawRecord, InventorySource, Listing, Vehicle, VehicleChange, VehiclePhoto
from tests.conftest import SAMPLE_CSV, login, run_worker
from tests.helpers import import_csv, vehicle_by_stock


def test_csv_import_normalizes_and_preserves_raw(world):
    ctx, run, src = import_csv(world["a"].id)
    assert run.status == "succeeded"
    assert run.counts["created"] == 3
    camry = vehicle_by_stock(ctx, "B200")
    assert (camry.make, camry.model, camry.year, camry.mileage, camry.price_cents) == ("Toyota", "Camry", 2019, 45000, 1899500)
    assert camry.transmission == "automatic" and camry.drivetrain == "FWD" and camry.fuel_type == "gasoline"
    assert camry.features == ["Backup Camera", "Apple CarPlay"]
    f150 = vehicle_by_stock(ctx, "C300")
    assert f150.drivetrain == "4WD" and f150.model == "F-150"
    raw = ctx.db.scalars(select(ImportRawRecord).where(ImportRawRecord.run_id == run.id)).all()
    assert len(raw) == 3 and raw[0].raw["Price"] == "$5,995"  # source data preserved verbatim
    photos = ctx.db.scalars(select(VehiclePhoto).where(VehiclePhoto.vehicle_id == vehicle_by_stock(ctx, "A100").id)).all()
    assert [p.position for p in photos] == [0, 1]
    # a listing row exists per vehicle and generation was queued
    assert ctx.db.scalar(select(Listing).where(Listing.vehicle_id == camry.id)).status == "imported"


def test_malformed_records_are_flagged_not_guessed(world):
    csv = """Stock #,Year,Make,Model,Miles,Price,Trans
X1,20X9,Honda,Civic,abc,call for price,teleport
,2015,Kia,Soul,1000,9000,auto
X3,2016,Mazda,3,"40,000 mi",12000,automatic,extra-cell
"""
    ctx, run, _ = import_csv(world["a"].id, csv)
    assert run.status == "partial" and run.counts["rejected"] == 1
    x1 = vehicle_by_stock(ctx, "X1")
    codes = {i["code"] for i in x1.validation_issues}
    assert {"invalid_year", "unparseable_mileage", "unparseable_price", "unrecognized_value", "missing_required"} <= codes
    assert x1.year is None and x1.mileage is None and x1.price_cents is None and x1.transmission is None
    assert x1.validation_status == "invalid"
    lst = ctx.db.scalar(select(Listing).where(Listing.vehicle_id == x1.id))
    assert lst.status == "needs_validation"
    x3 = vehicle_by_stock(ctx, "X3")
    assert x3.mileage == 40000
    assert any(i["code"] == "extra_cells" for i in x3.validation_issues)


def test_contradictory_data_detected(world):
    csv = """Stock #,VIN,Year,Make,Model,Miles,Price,Condition
V1,1HGCM82633A004352,2012,Honda,Accord,145000,5995,used
V2,,2020,Ford,Escape,50000,15000,new
"""
    ctx, run, _ = import_csv(world["a"].id, csv)
    v1, v2 = vehicle_by_stock(ctx, "V1"), vehicle_by_stock(ctx, "V2")
    assert any(i["code"] == "vin_year_mismatch" for i in v1.validation_issues)  # VIN says 2003
    assert any(i["code"] == "condition_mileage_conflict" for i in v2.validation_issues)


def test_vin_check_digit():
    assert check_digit("1HGCM82633A004352") == "3"
    assert vin_problems("1HGCM82633A004352") == []
    assert vin_problems("1HGCM82643A004352")  # bad check digit
    assert vin_problems("1HGCM8263IA004352")  # invalid letter I


def test_duplicates_in_file_and_across_sources(world):
    csv = SAMPLE_CSV + "A100,1HGCM82633A004352,2003,Honda,Accord,EX,Sedan,145000,5995,Silver,Gray,Automatic,FWD,Gas,,,,,,,Used\n"
    ctx, run, _ = import_csv(world["a"].id, csv)
    assert run.counts["duplicate_in_file"] == 1
    assert len(ctx.db.scalars(select(Vehicle)).all()) == 3
    other = InventorySource(client_id=world["a"].id, name="Website feed", source_type="csv_upload")
    ctx.db.add(other)
    ctx.db.commit()
    ctx2, run2, _ = import_csv(world["a"].id, "VIN,Year,Make,Model,Price\n1HGCM82633A004352,2003,Honda,Accord,5995\n", source=other)
    dup = ctx2.db.scalar(select(Vehicle).where(Vehicle.source_id == other.id))
    assert any(i["code"] == "duplicate_vin" for i in dup.validation_issues)


def test_reimport_is_idempotent_and_detects_price_change(world):
    ctx, run1, src = import_csv(world["a"].id)
    _, run2, _ = import_csv(world["a"].id, source=src)
    assert run2.counts["unchanged"] == 3 and run2.counts["updated"] == 0
    changed = SAMPLE_CSV.replace("18995", "17995")
    ctx3, run3, _ = import_csv(world["a"].id, changed, source=src)
    assert run3.counts["price_changed"] == 1
    camry = vehicle_by_stock(ctx3, "B200")
    assert camry.price_cents == 1799500 and camry.version == 2
    ch = ctx3.db.scalars(select(VehicleChange).where(VehicleChange.vehicle_id == camry.id, VehicleChange.change_type == "price_changed")).all()
    assert ch[0].changes["price"] == {"before": 1899500, "after": 1799500}


def test_sold_and_removed_detection_with_mass_removal_safeguard(world):
    ctx, _, src = import_csv(world["a"].id)
    lines = SAMPLE_CSV.strip().splitlines()
    # Accord marked sold, F-150 absent from full snapshot -> removed.
    sold_csv = "\n".join([lines[0] + ",Status", lines[1] + ",Sold", lines[2] + ",Available"]) + "\n"
    ctx2, run, _ = import_csv(world["a"].id, sold_csv, source=src)
    assert vehicle_by_stock(ctx2, "A100").inventory_status == "sold"
    assert vehicle_by_stock(ctx2, "C300").inventory_status == "removed"
    assert run.counts["removed"] == 1
    # A truncated file with 1 of 6 vehicles must not remove the other 5.
    many = lines[0] + "\n" + "\n".join(f"Z{i},,2015,Kia,Soul,Base,Hatchback,1000,9000,Red,Black,auto,fwd,gas,,https://img.example/z.jpg,1 St,Sugar Land,TX,77478,Used"
                                       for i in range(6)) + "\n"
    ctx3, _, src2 = import_csv(world["b"].id, many)
    ctx4, run4, _ = import_csv(world["b"].id, lines[0] + "\n" + many.splitlines()[1] + "\n", source=src2)
    assert run4.counts.get("removal_blocked") == 5
    assert run4.status == "partial"
    assert len(ctx4.db.scalars(select(Vehicle).where(Vehicle.inventory_status == "available")).all()) == 6


def test_xlsx_upload_via_api(world, client):
    wb = Workbook()
    ws = wb.active
    ws.append(["Stock Number", "Year", "Make", "Model", "Mileage", "Price", "Photos"])
    ws.append(["S1", 2017, "Nissan", "Altima", 61000, 11995, "https://img.example/s1.jpg"])
    buf = io.BytesIO()
    wb.save(buf)
    h = login(client, "owner@alpha.test")
    r = client.post(f"/api/clients/{world['a'].id}/imports/upload", headers=h,
                    files={"file": ("inventory.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 201, r.text
    assert r.json()["counts"]["created"] == 1
    v = client.get(f"/api/clients/{world['a'].id}/vehicles", headers=h).json()["items"][0]
    assert v["make"] == "Nissan" and v["mileage"] == 61000 and v["price"] == 11995


def test_authorized_feed_sync_through_worker(world, client):
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"vehicles": [{"id": "F1", "year": 2020, "make": "Honda", "model": "Civic", "price": "17000",
                                                       "mileage": 30000, "images": ["https://img.example/f1.jpg"]}]})
    feeds.set_transport(httpx.MockTransport(handler))
    try:
        h = login(client, "owner@alpha.test")
        # Feeds must be https and record who authorised them.
        bad = client.post(f"/api/clients/{world['a'].id}/sources", headers=h, json={
            "name": "DMS feed", "source_type": "feed_api", "config": {"url": "https://dms.example/feed.json", "format": "json"}})
        assert bad.status_code == 400
        r = client.post(f"/api/clients/{world['a'].id}/sources", headers=h, json={
            "name": "DMS feed", "source_type": "feed_api", "config": {"url": "https://dms.example/feed.json", "format": "json"},
            "secret": "dealer-issued-token", "schedule_minutes": 60,
            "authorization_note": "Owner J. Doe emailed DMS export authorization 2026-10-01"})
        assert r.status_code == 201, r.text
        assert "dealer-issued-token" not in r.text
        sid = r.json()["id"]
        assert client.post(f"/api/clients/{world['a'].id}/sources/{sid}/sync", headers=h).status_code == 202
        run_worker()
        assert seen["auth"] == "Bearer dealer-issued-token"
        items = client.get(f"/api/clients/{world['a'].id}/vehicles", headers=h).json()["items"]
        assert items[0]["model"] == "Civic"
    finally:
        feeds.set_transport(None)


def test_stale_scan_flags_unconfirmed_availability(world):
    from datetime import timedelta
    from app.core.time import utcnow
    ctx, _, src = import_csv(world["a"].id)
    for v in ctx.db.scalars(select(Vehicle)).all():
        v.last_seen_at = utcnow() - timedelta(hours=100)
    ctx.db.commit()
    from app.sync.jobs import handle_stale_scan
    from app.db import SessionLocal
    s = SessionLocal()
    res = handle_stale_scan(s, None, {})
    s.commit()
    assert res["stale_flagged"] == 3
    ctx.db.expire_all()
    assert all(v.stale for v in ctx.db.scalars(select(Vehicle)).all())


def test_feed_urls_to_internal_addresses_are_rejected(world, client):
    h = login(client, "owner@alpha.test")
    for url in ("https://127.0.0.1/feed.csv", "https://169.254.169.254/latest", "https://localhost/feed", "http://dms.example.com/f.csv"):
        r = client.post(f"/api/clients/{world['a'].id}/sources", headers=h, json={
            "name": "x", "source_type": "feed_api", "config": {"url": url}, "authorization_note": "test"})
        assert r.status_code == 400, url

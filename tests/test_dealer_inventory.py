"""Phase 2 inventory ingestion: imports, malformed rows, duplicates, changes, sold/removed, feeds, stale data."""

from __future__ import annotations

import io
from datetime import timedelta

import httpx
import pytest
from openpyxl import Workbook
from sqlalchemy import select

from dealerflow.db import utcnow
from dealerflow.integrations import http as http_mod
from dealerflow.models_dealer import ImportRawRecord, InventorySource, Listing, Vehicle, VehicleChange, VehiclePhoto
from dealerflow.services.inventory.vin import check_digit, vin_problems
from tests.dealer_helpers import (SAMPLE_CSV, dealer_login, drain, import_csv, make_client, make_dealer_user, page_csrf,
                                  vehicle_by_stock)

REAL_CLIENT = httpx.Client


@pytest.fixture
def cid():
    return make_client()


def test_csv_import_normalizes_and_preserves_raw(cid):
    ctx, run, src = import_csv(cid)
    assert run.status == "succeeded" and run.counts["created"] == 3
    camry = vehicle_by_stock(ctx, "B200")
    assert (camry.make, camry.model, camry.year, camry.mileage, camry.price_cents) == ("Toyota", "Camry", 2019, 45000, 1899500)
    assert (camry.transmission, camry.drivetrain, camry.fuel_type) == ("automatic", "FWD", "gasoline")
    assert camry.features == ["Backup Camera", "Apple CarPlay"]
    f150 = vehicle_by_stock(ctx, "C300")
    assert f150.drivetrain == "4WD" and f150.model == "F-150"
    raw = ctx.db.scalars(select(ImportRawRecord).where(ImportRawRecord.run_id == run.id)).all()
    assert len(raw) == 3 and raw[0].raw["Price"] == "$5,995"  # source data kept verbatim
    photos = ctx.db.scalars(select(VehiclePhoto).where(VehiclePhoto.vehicle_id == vehicle_by_stock(ctx, "A100").id)).all()
    assert [p.position for p in photos] == [0, 1]
    assert ctx.db.scalar(select(Listing).where(Listing.vehicle_id == camry.id)).status == "imported"
    ctx.close()


def test_malformed_records_are_flagged_not_guessed(cid):
    csv = """Stock #,Year,Make,Model,Miles,Price,Trans
X1,20X9,Honda,Civic,abc,call for price,teleport
,2015,Kia,Soul,1000,9000,auto
X3,2016,Mazda,3,"40,000 mi",12000,automatic,extra-cell
"""
    ctx, run, _ = import_csv(cid, csv)
    assert run.status == "partial" and run.counts["rejected"] == 1
    x1 = vehicle_by_stock(ctx, "X1")
    codes = {i["code"] for i in x1.validation_issues}
    assert {"invalid_year", "unparseable_mileage", "unparseable_price", "unrecognized_value", "missing_required"} <= codes
    assert x1.year is None and x1.mileage is None and x1.price_cents is None and x1.transmission is None
    assert x1.validation_status == "invalid"
    assert ctx.db.scalar(select(Listing).where(Listing.vehicle_id == x1.id)).status == "needs_validation"
    x3 = vehicle_by_stock(ctx, "X3")
    assert x3.mileage == 40000 and any(i["code"] == "extra_cells" for i in x3.validation_issues)
    ctx.close()


def test_contradictory_data_detected(cid):
    ctx, _, _ = import_csv(cid, "Stock #,VIN,Year,Make,Model,Miles,Price,Condition\n"
                                "V1,1HGCM82633A004352,2012,Honda,Accord,145000,5995,used\nV2,,2020,Ford,Escape,50000,15000,new\n")
    assert any(i["code"] == "vin_year_mismatch" for i in vehicle_by_stock(ctx, "V1").validation_issues)  # VIN says 2003
    assert any(i["code"] == "condition_mileage_conflict" for i in vehicle_by_stock(ctx, "V2").validation_issues)
    ctx.close()


def test_vin_check_digit():
    assert check_digit("1HGCM82633A004352") == "3" and vin_problems("1HGCM82633A004352") == []
    assert vin_problems("1HGCM82643A004352") and vin_problems("1HGCM8263IA004352")


def test_duplicates_in_file_and_across_sources(cid):
    dup = SAMPLE_CSV + "A100,1HGCM82633A004352,2003,Honda,Accord,EX,Sedan,145000,5995,Silver,Gray,Automatic,FWD,Gas,,,,,,,Used\n"
    ctx, run, _ = import_csv(cid, dup)
    assert run.counts["duplicate_in_file"] == 1 and len(ctx.db.scalars(select(Vehicle)).all()) == 3
    ctx.close()
    ctx2, _, other = import_csv(cid, "VIN,Year,Make,Model,Price\n1HGCM82633A004352,2003,Honda,Accord,5995\n", source_name="Website feed")
    v = ctx2.db.scalar(select(Vehicle).where(Vehicle.source_id == other.id))
    assert any(i["code"] == "duplicate_vin" for i in v.validation_issues)
    ctx2.close()


def test_reimport_is_idempotent_and_detects_price_change(cid):
    import_csv(cid)[0].close()
    ctx2, run2, _ = import_csv(cid)
    assert run2.counts["unchanged"] == 3 and run2.counts["updated"] == 0
    ctx2.close()
    ctx3, run3, _ = import_csv(cid, SAMPLE_CSV.replace("18995", "17995"))
    assert run3.counts["price_changed"] == 1
    camry = vehicle_by_stock(ctx3, "B200")
    assert camry.price_cents == 1799500 and camry.version == 2
    ch = ctx3.db.scalar(select(VehicleChange).where(VehicleChange.vehicle_id == camry.id, VehicleChange.change_type == "price_changed"))
    assert ch.changes["price"] == {"before": 1899500, "after": 1799500}
    ctx3.close()


def test_sold_removed_and_mass_removal_safeguard(cid):
    import_csv(cid)[0].close()
    lines = SAMPLE_CSV.strip().splitlines()
    ctx2, run, _ = import_csv(cid, "\n".join([lines[0] + ",Status", lines[1] + ",Sold", lines[2] + ",Available"]) + "\n")
    assert vehicle_by_stock(ctx2, "A100").inventory_status == "sold"
    assert vehicle_by_stock(ctx2, "C300").inventory_status == "removed" and run.counts["removed"] == 1
    ctx2.close()
    other = make_client("Bravo Motors")
    many = lines[0] + "\n" + "\n".join(f"Z{i},,2015,Kia,Soul,Base,Hatchback,1000,9000,Red,Black,auto,fwd,gas,,https://img.example/z.jpg,1 St,Sugar Land,TX,77478,Used"
                                       for i in range(6)) + "\n"
    import_csv(other, many)[0].close()
    ctx4, run4, _ = import_csv(other, lines[0] + "\n" + many.splitlines()[1] + "\n")
    assert run4.counts.get("removal_blocked") == 5 and run4.status == "partial"
    assert len(ctx4.db.scalars(select(Vehicle).where(Vehicle.inventory_status == "available")).all()) == 6
    ctx4.close()


def test_xlsx_upload_through_the_page(cid, client):
    make_dealer_user(cid, "owner@alpha.example")
    dealer_login(client, "owner@alpha.example")
    wb = Workbook()
    ws = wb.active
    ws.append(["Stock Number", "Year", "Make", "Model", "Mileage", "Price", "Photos"])
    ws.append(["S1", 2017, "Nissan", "Altima", 61000, 11995, "https://img.example/s1.jpg"])
    buf = io.BytesIO()
    wb.save(buf)
    csrf = page_csrf(client, f"/dealers/{cid}/imports")
    r = client.post(f"/dealers/{cid}/imports", data={"csrf": csrf},
                    files={"file": ("inventory.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    page = client.get(f"/dealers/{cid}")
    assert "Nissan" in page.text and "Altima" in page.text


def test_authorised_feed_sync_through_worker(cid, monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"vehicles": [{"id": "F1", "year": 2020, "make": "Honda", "model": "Civic", "price": "17000",
                                                       "mileage": 30000, "images": ["https://img.example/f1.jpg"]}]})
    monkeypatch.setattr(http_mod.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("DF_FEED_ALPHA_DMS", "dealer-issued-token")
    monkeypatch.setattr("dealerflow.services.netguard._resolve", lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 443))])
    from dealerflow.services import dealer_jobs
    from dealerflow.services.inventory.feeds import validate_feed_config
    from dealerflow.tenancy import system_context
    ctx = system_context(cid)
    cfg = {"url": "https://dms.example.com/feed.json", "format": "json"}
    with pytest.raises(Exception):
        validate_feed_config("feed_api", cfg, None)  # authorisation note is required
    validate_feed_config("feed_api", cfg, "Owner J. Doe authorised the DMS export on 2026-10-01", "DF_FEED_ALPHA_DMS")
    src = InventorySource(client_id=cid, name="DMS feed", source_type="feed_api", config=cfg, secret_env="DF_FEED_ALPHA_DMS",
                          schedule_minutes=60, authorization_note="Owner J. Doe authorised the DMS export on 2026-10-01")
    ctx.db.add(src)
    ctx.db.flush()
    dealer_jobs.sync_now(ctx.db, src)
    ctx.db.commit()
    ctx.close()
    drain()
    assert seen["auth"] == "Bearer dealer-issued-token"
    ctx = system_context(cid)
    assert ctx.db.scalar(select(Vehicle).where(Vehicle.external_id == "F1")).model == "Civic"
    ctx.close()


def test_feed_urls_to_internal_addresses_are_rejected():
    from dealerflow.services.dealer_errors import BadRequest
    from dealerflow.services.inventory.feeds import validate_feed_config
    for url in ("https://127.0.0.1/feed.csv", "https://169.254.169.254/latest", "https://localhost/feed", "http://dms.example.com/f.csv"):
        with pytest.raises(BadRequest):
            validate_feed_config("feed_api", {"url": url}, "authorised")
    with pytest.raises(BadRequest):
        validate_feed_config("feed_api", {"url": "https://dms.example.com/f.csv"}, "ok", secret_env="DF_SECRET_KEY")


def test_stale_scan_flags_unconfirmed_availability(cid):
    ctx, _, _ = import_csv(cid)
    for v in ctx.db.scalars(select(Vehicle)).all():
        v.last_seen_at = utcnow() - timedelta(hours=100)
    ctx.db.commit()
    ctx.close()
    from dealerflow.db import session_scope
    from dealerflow.services.dealer_jobs import run_stale_scan
    with session_scope() as db:
        assert run_stale_scan(db, {})["stale_flagged"] == 3
    from dealerflow.tenancy import system_context
    ctx = system_context(cid)
    assert all(v.stale for v in ctx.db.scalars(select(Vehicle)).all())
    ctx.close()

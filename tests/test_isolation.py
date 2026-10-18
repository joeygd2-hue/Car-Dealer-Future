"""One dealership must never see or change another dealership's data."""
from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.db import TenantViolation, tenant_session
from app.listings import service
from app.models import Listing, Vehicle
from tests.conftest import login
from tests.helpers import import_csv, listing_for, vehicle_by_stock


@pytest.fixture
def two_dealers(world):
    ca, _, _ = import_csv(world["a"].id)
    cb, _, _ = import_csv(world["b"].id)
    va, vb = vehicle_by_stock(ca, "B200"), vehicle_by_stock(cb, "B200")
    la, lb = listing_for(ca, va), listing_for(cb, vb)
    service.generate_listing(cb, lb.id)
    cb.db.commit()
    return {"va": va.id, "vb": vb.id, "la": la.id, "lb": lb.id}


def test_api_blocks_cross_dealer_reads_and_writes(world, client, two_dealers):
    a, b = world["a"].id, world["b"].id
    h = login(client, "owner@alpha.test")
    assert client.get(f"/api/clients/{a}/vehicles", headers=h).status_code == 200
    # Another dealership's routes: indistinguishable from "does not exist".
    for path in (f"/api/clients/{b}/vehicles", f"/api/clients/{b}/listings", f"/api/clients/{b}/reports/summary",
                 f"/api/clients/{b}/listing-settings", f"/api/clients/{b}/imports", f"/api/clients/{b}/audit"):
        assert client.get(path, headers=h).status_code == 404, path
    # B's object ids through A's scope are invisible too.
    assert client.get(f"/api/clients/{a}/vehicles/{two_dealers['vb']}", headers=h).status_code == 404
    assert client.get(f"/api/clients/{a}/listings/{two_dealers['lb']}", headers=h).status_code == 404
    r = client.post(f"/api/clients/{a}/listings/{two_dealers['lb']}/generate", headers=h, json={})
    assert r.status_code == 404
    r = client.post(f"/api/clients/{a}/vehicles/{two_dealers['vb']}/status", headers=h, json={"status": "sold"})
    assert r.status_code == 404
    r = client.post(f"/api/clients/{b}/imports/upload", headers=h, files={"file": ("x.csv", b"Stock #,Year,Make,Model\n1,2010,a,b\n")})
    assert r.status_code == 404
    hb = login(client, "owner@bravo.test")
    v = client.get(f"/api/clients/{b}/vehicles/{two_dealers['vb']}", headers=hb).json()
    assert v["inventory_status"] == "available"


def test_listing_list_only_contains_own_rows(world, client, two_dealers):
    h = login(client, "owner@alpha.test")
    ids = {x["id"] for x in client.get(f"/api/clients/{world['a'].id}/listings", headers=h).json()["items"]}
    assert two_dealers["la"] in ids and two_dealers["lb"] not in ids


def test_roles_enforced_server_side(world, client):
    a = world["a"].id
    viewer = login(client, "viewer@alpha.test")
    assert client.get(f"/api/clients/{a}/vehicles", headers=viewer).status_code == 200
    assert client.post(f"/api/clients/{a}/vehicles", headers=viewer, json={"year": 2010, "make": "Kia", "model": "Rio"}).status_code == 403
    assert client.put(f"/api/clients/{a}/listing-settings", headers=viewer, json={"brand_voice": "friendly"}).status_code == 403
    op = login(client, "op@agency.test")
    assert client.put(f"/api/clients/{a}/listing-settings", headers=op, json={"brand_voice": "friendly"}).status_code == 403
    assert client.get("/api/outreach/dashboard", headers=viewer).status_code == 403  # agency-only feature
    assert client.get(f"/api/clients/{a}/vehicles").status_code == 401


def test_orm_tenant_session_filters_and_refuses_cross_tenant_writes(world, two_dealers):
    s = tenant_session(world["a"].id)
    rows = s.scalars(select(Vehicle)).all()
    assert rows and all(v.client_id == world["a"].id for v in rows)
    assert s.get(Vehicle, two_dealers["vb"]) is None or s.get(Vehicle, two_dealers["vb"]).client_id != world["a"].id
    assert s.scalar(select(Listing).where(Listing.id == two_dealers["lb"])) is None
    # bulk update through a tenant session only touches own rows
    s.execute(update(Vehicle).values(stale=True))
    s.commit()
    sb = tenant_session(world["b"].id)
    assert not any(v.stale for v in sb.scalars(select(Vehicle)).all())
    # writing a row for another dealership is refused
    s.add(Vehicle(client_id=world["b"].id, source_id="x", external_id="x", content_fingerprint="x", photo_fingerprint="x"))
    with pytest.raises(TenantViolation):
        s.flush()
    s.rollback()
    v = s.scalars(select(Vehicle)).first()
    v.client_id = world["b"].id
    with pytest.raises(TenantViolation):
        s.flush()
    s.close()
    sb.close()


def test_composite_foreign_keys_prevent_cross_tenant_links(world, two_dealers, session):
    from sqlalchemy.exc import IntegrityError
    # A listing for dealer A pointing at dealer B's vehicle violates the (client_id, vehicle_id) FK.
    session.add(Listing(client_id=world["a"].id, vehicle_id=two_dealers["vb"], status="imported"))
    with pytest.raises(IntegrityError):
        session.flush()

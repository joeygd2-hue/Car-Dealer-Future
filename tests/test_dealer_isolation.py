"""One dealership must never see or change another dealership's data; dealership users never see staff pages."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from dealerflow.db import session_factory
from dealerflow.models_dealer import Listing, Vehicle
from dealerflow.services.listings import service
from dealerflow.tenancy import TenantViolation, tenant_session
from tests.conftest import login, make_user
from tests.dealer_helpers import (dealer_login, import_csv, listing_for, make_client, make_dealer_user, page_csrf, vehicle_by_stock)


@pytest.fixture
def two(db):
    a, b = make_client("Alpha Auto"), make_client("Bravo Motors")
    ca, _, _ = import_csv(a)
    cb, _, _ = import_csv(b)
    va, vb = vehicle_by_stock(ca, "B200"), vehicle_by_stock(cb, "B200")
    lb = listing_for(cb, vb)
    service.generate_listing(cb, lb.id)
    cb.db.commit()
    out = {"a": a, "b": b, "va": va.id, "vb": vb.id, "la": listing_for(ca, va).id, "lb": lb.id}
    ca.close()
    cb.close()
    make_dealer_user(a, "owner@alpha.example")
    make_dealer_user(a, "viewer@alpha.example", role="dealer_viewer")
    return out


def test_dealer_user_cannot_reach_another_dealership(client, two):
    a, b = two["a"], two["b"]
    dealer_login(client, "owner@alpha.example")
    assert client.get(f"/dealers/{a}").status_code == 200
    for path in (f"/dealers/{b}", f"/dealers/{b}/listings", f"/dealers/{b}/reports", f"/dealers/{b}/settings", f"/dealers/{b}/imports",
                 f"/api/v1/dealers/{b}/vehicles", f"/dealers/{a}/vehicles/{two['vb']}", f"/dealers/{a}/listings/{two['lb']}",
                 f"/api/v1/dealers/{a}/listings/{two['lb']}"):
        assert client.get(path).status_code == 404, path
    csrf = page_csrf(client, f"/dealers/{a}")
    assert client.post(f"/dealers/{a}/listings/{two['lb']}/generate", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert "not found" in client.get(f"/dealers/{a}").text.lower()  # flashed error, nothing changed in B
    r = client.post(f"/dealers/{b}/vehicles/{two['vb']}/status", data={"csrf": csrf, "status": "sold"}, follow_redirects=False)
    assert r.status_code == 404
    s = tenant_session(b)
    assert s.get(Vehicle, two["vb"]).inventory_status == "available"
    s.close()


def test_dealer_user_cannot_open_staff_pages(client, two):
    dealer_login(client, "owner@alpha.example")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/dealers/{two['a']}"
    for path in ("/prospects", "/agent", "/outreach", "/settings", "/dealers/" if False else "/audit", "/api/v1/prospects"):
        assert client.get(path).status_code == 403, path
    assert client.get("/dealers", follow_redirects=False).headers["location"] == f"/dealers/{two['a']}"


def test_roles_enforced_server_side(client, two):
    a = two["a"]
    dealer_login(client, "viewer@alpha.example")
    csrf = page_csrf(client, f"/dealers/{a}")
    assert client.post(f"/dealers/{a}/vehicles/new", data={"csrf": csrf, "year": "2010", "make": "Kia", "model": "Rio"}).status_code == 403
    assert client.post(f"/dealers/{a}/settings", data={"csrf": csrf, "brand_voice": "friendly"}).status_code == 403
    assert client.post(f"/dealers/{a}/listings/{two['la']}/approve", data={"csrf": csrf, "revision_id": "1"}).status_code == 403
    assert client.post(f"/dealers/{a}/settings", data={"brand_voice": "friendly"}).status_code == 403  # CSRF also required


def test_staff_can_open_any_workspace_but_viewer_staff_cannot_edit(client, db, two):
    make_user(db, "viewer@agency.example", role="viewer")
    login(client, "viewer@agency.example")
    assert client.get(f"/dealers/{two['b']}").status_code == 200
    csrf = page_csrf(client, f"/dealers/{two['b']}")
    assert client.post(f"/dealers/{two['b']}/vehicles/new", data={"csrf": csrf, "year": "2010", "make": "Kia", "model": "Rio"}).status_code == 403


def test_orm_tenant_session_filters_and_refuses_cross_tenant_writes(two):
    s = tenant_session(two["a"])
    rows = s.scalars(select(Vehicle)).all()
    assert rows and all(v.client_id == two["a"] for v in rows)
    assert s.scalar(select(Listing).where(Listing.id == two["lb"])) is None
    s.execute(update(Vehicle).values(stale=True))
    s.commit()
    sb = tenant_session(two["b"])
    assert not any(v.stale for v in sb.scalars(select(Vehicle)).all())
    s.add(Vehicle(client_id=two["b"], source_id=1, external_id="x", content_fingerprint="x", photo_fingerprint="x"))
    with pytest.raises(TenantViolation):
        s.flush()
    s.rollback()
    v = s.scalars(select(Vehicle)).first()
    v.client_id = two["b"]
    with pytest.raises(TenantViolation):
        s.flush()
    s.close()
    sb.close()


def test_composite_foreign_keys_block_cross_tenant_links(two):
    s = session_factory()()
    s.add(Listing(client_id=two["a"], vehicle_id=two["vb"], status="imported"))
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()
    s.close()

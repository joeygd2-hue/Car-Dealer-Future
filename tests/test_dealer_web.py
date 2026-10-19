"""Phase 2 end-to-end over HTTP: staff creates a dealership from a won prospect, a dealership user imports inventory,
the worker drafts listings, the dealer batch-approves, publishes manually to Marketplace and through the simulated
API channel, then checks reports, export and the audit trail. Fixture data only."""

from __future__ import annotations

from sqlalchemy import select

from dealerflow.db import session_scope
from dealerflow.models import AuditLog, Prospect
from dealerflow.models import Client
from dealerflow.models_dealer import Listing
from dealerflow.security import new_api_key
from tests.conftest import login, make_user
from tests.dealer_helpers import SAMPLE_CSV, dealer_login, drain, page_csrf


def test_full_dealership_workflow(db, client):
    from fastapi.testclient import TestClient
    from dealerflow.web.app import create_app

    admin = make_user(db, "admin@agency.example", role="admin")
    with session_scope() as s:
        p = Prospect(name="Fixture Motors", normalized_name="fixture motors", stage="won", city="Sugar Land", state="TX", simulated=True)
        s.add(p)
        s.flush()
        pid = p.id
    csrf = login(client, "admin@agency.example")
    r = client.post("/clients", data={"csrf": csrf, "prospect_id": str(pid)}, follow_redirects=False)  # one creation path
    assert r.status_code == 303 and r.headers["location"].startswith("/clients/")
    cid = int(r.headers["location"].rsplit("/", 1)[-1])
    assert f'href="/dealers/{cid}"' in client.get("/dealers").text
    with session_scope() as s:
        c = s.get(Client, cid)
        assert c.prospect_id == pid and c.simulated  # linked to the CRM record it came from
    csrf = page_csrf(client, f"/dealers/{cid}/settings")
    r = client.post(f"/dealers/{cid}/users", data={"csrf": csrf, "email": "owner@fixture.example", "role": "dealer_admin",
                                                   "password": "correct horse battery"}, follow_redirects=False)
    assert r.status_code == 303

    dealer = TestClient(create_app())
    dealer_login(dealer, "owner@fixture.example")
    csrf = page_csrf(dealer, f"/dealers/{cid}/settings")
    r = dealer.post(f"/dealers/{cid}/settings", data={
        "csrf": csrf, "brand_voice": "friendly", "dealership_name": "Fixture Motors", "phone": "(281) 555-0100", "email": "sales@fixture.example",
        "hours_en": "Mon-Sat 9-7", "hours_es": "Lun-Sáb 9-7", "lang_en": "yes", "lang_es": "yes", "require_both_languages": "yes",
        "required_fields": "year, make, model, price, mileage, photos", "enabled_channels": "facebook_marketplace, mock",
        "disclaimers": "Price excludes tax, title and license. || El precio no incluye impuestos, título ni placas."}, follow_redirects=False)
    assert r.status_code == 303
    csrf = page_csrf(dealer, f"/dealers/{cid}/imports")
    r = dealer.post(f"/dealers/{cid}/imports", data={"csrf": csrf}, files={"file": ("inventory.csv", SAMPLE_CSV.encode(), "text/csv")},
                    follow_redirects=False)
    assert r.status_code == 303 and "/imports/" in r.headers["location"]
    assert "created: 3" in dealer.get(r.headers["location"]).text
    drain()  # worker generates drafts

    page = dealer.get(f"/dealers/{cid}/listings?status=awaiting_approval")
    assert page.text.count('name="listing_id"') == 3
    with session_scope() as s:
        ids = [str(i) for i in s.scalars(select(Listing.id).where(Listing.client_id == cid))]
    csrf = page_csrf(dealer, f"/dealers/{cid}/listings")
    preview = dealer.post(f"/dealers/{cid}/listings/batch-preview", data={"csrf": csrf, "listing_id": ids})
    assert "3 of 3 eligible" in preview.text
    r = dealer.post(f"/dealers/{cid}/listings/batch-approve", data={"csrf": csrf, "listing_id": ids, "expected_count": "3",
                                                                    "confirm": "yes", "acknowledge_all_warnings": "yes"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Approved 3 listing(s)" in dealer.get(f"/dealers/{cid}/listings").text

    detail = dealer.get(f"/dealers/{cid}/listings/{ids[0]}")
    assert "Español" in detail.text and "Open publishing package" in detail.text
    pub_links = [line.split('href="')[1].split('"')[0] for line in detail.text.split("\n") if "/publications/" in line and 'href="' in line]
    fb = next(link for link in pub_links if dealer.get(link).text.count("Marketplace") > 0 and "Record publication" in dealer.get(link).text)
    pkg = dealer.get(fb).text
    assert "Checklist" in pkg and "Spanish copy" in pkg and "Nothing here is sent to Facebook" in pkg
    csrf = page_csrf(dealer, fb)
    r = dealer.post(f"{fb}/confirm-manual", data={"csrf": csrf, "url": "https://www.facebook.com/marketplace/item/99887766554433/",
                                                  "attested": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and "confirmed by staff attestation" in dealer.get(fb).text
    mock = next(link for link in pub_links if link != fb)
    r = dealer.post(f"{mock}/publish", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    drain()
    assert "confirmed by API" in dealer.get(mock).text

    report = dealer.get(f"/dealers/{cid}/reports").text
    assert "Published manually (staff-attested)" in report
    csv = dealer.get(f"/dealers/{cid}/export.csv?lang=es")
    assert csv.status_code == 200 and "millas" in csv.text

    key, key_hash = new_api_key()
    with session_scope() as s:
        s.get(type(admin), admin.id).api_key_hash = key_hash
    api = TestClient(create_app())
    summary = api.get(f"/api/v1/dealers/{cid}/reports/summary", headers={"Authorization": f"Bearer {key}"}).json()
    assert summary["publishing"]["published_confirmed_manually"] == 1
    assert summary["publishing"]["published_confirmed_via_api"] == 0  # the simulated channel is not a real API integration
    with session_scope() as s:
        actions = set(s.scalars(select(AuditLog.action)))
    assert {"client.created", "client_user.created", "inventory.imported", "listing.generated", "listing.approved",
            "publication.manual_confirmed"} <= actions


def test_every_dealer_page_renders(db, client):
    from tests.dealer_helpers import import_csv, make_client
    make_user(db, "admin@agency.example", role="admin")
    cid = make_client()
    import_csv(cid)[0].close()
    drain()
    login(client, "admin@agency.example")
    for path in ("/dealers", f"/dealers/{cid}", f"/dealers/{cid}/vehicles/new", f"/dealers/{cid}/vehicles/1", f"/dealers/{cid}/listings",
                 f"/dealers/{cid}/listings/1", f"/dealers/{cid}/publishing", f"/dealers/{cid}/imports", f"/dealers/{cid}/imports/1",
                 f"/dealers/{cid}/reports", f"/dealers/{cid}/settings", "/"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    assert 'href="/dealers"' in client.get("/").text  # staff nav links to the workspaces

"""Seams between Phase 2 (dealership workspace) and Phase 4 (client operations) after the branches were integrated."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from dealerflow.db import session_scope
from dealerflow.models import Client, CostEntry, OnboardingStep, Prospect, SupportTicket, User
from dealerflow.models_dealer import Listing, Vehicle, VehiclePhoto
from dealerflow.services import maintenance, tickets
from tests.conftest import login, make_user
from tests.dealer_helpers import dealer_login, drain, import_csv, make_client, make_dealer_user, page_csrf


def test_staff_created_dealership_gets_the_phase4_onboarding_and_stops_outreach(db, client):
    admin = make_user(db)
    db.add(Prospect(name="Fixture Motors (SIMULATED)", normalized_name="fixture motors", email="sales@fixture.example", simulated=True))
    db.commit()
    pid = db.scalar(select(Prospect.id).where(Prospect.name == "Fixture Motors (SIMULATED)"))
    csrf = login(client, admin.email)
    r = client.post("/dealers", data={"csrf": csrf, "prospect_id": str(pid)}, follow_redirects=False)
    assert r.status_code == 303
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    with session_scope() as s:
        c = s.get(Client, cid)
        assert c.slug == "fixture-motors-simulated" and c.simulated and c.status == "onboarding"
        assert s.scalar(select(func.count()).select_from(OnboardingStep).where(OnboardingStep.client_id == cid)) > 0
    assert client.get(f"/clients/{cid}").status_code == 200  # the same client in the Phase 4 pages


def test_slug_is_unique_when_names_collide(db):
    a, b = Client(name="Same Name"), Client(name="Same Name")
    db.add_all([a, b])
    db.flush()
    assert {a.slug, b.slug} == {"same-name", "same-name-2"}


def test_one_client_user_reaches_workspace_and_portal_but_nothing_else(client):
    alpha, bravo = make_client("Alpha Auto"), make_client("Bravo Auto")
    make_dealer_user(alpha, "viewer@alpha.example", role="dealer_viewer")
    dealer_login(client, "viewer@alpha.example")
    assert client.get("/", follow_redirects=False).headers["location"] == f"/dealers/{alpha}"
    assert client.get(f"/dealers/{alpha}").status_code == 200
    assert client.get("/portal").status_code == 200
    assert client.get(f"/dealers/{bravo}").status_code == 404
    for path in ("/clients", "/finance", "/prospects", "/seo", "/api/v1/clients"):
        assert client.get(path, follow_redirects=False).status_code == 403, path
    csrf = page_csrf(client, f"/dealers/{alpha}")  # a viewer can look but not change inventory
    assert client.post(f"/dealers/{alpha}/vehicles/new", data={"csrf": csrf, "year": "2010", "make": "Kia", "model": "Rio"}).status_code == 403


def test_delete_client_removes_phase2_and_phase4_rows_and_anonymizes_users(db, tmp_path):
    keep, gone = make_client("Keep Motors"), make_client("Gone Motors")
    for cid in (keep, gone):
        import_csv(cid)
        make_dealer_user(cid, f"owner@{cid}.example")
    drain()  # listing drafts
    admin = make_user(db)
    tickets.create_ticket(db, client_id=gone, kind="other", title="t", description="d", actor=admin)
    db.add(CostEntry(incurred_on=date.today(), category="data", amount=1.5, note="vendor", client_id=gone))
    db.commit()

    path = maintenance.delete_client(db, gone, "Gone Motors", admin, str(tmp_path))
    db.commit()
    assert open(path).read().count("Gone Motors") >= 1  # exported before deletion
    assert db.get(Client, gone) is None
    for model in (Vehicle, VehiclePhoto, Listing, SupportTicket):
        assert db.scalar(select(func.count()).select_from(model).where(model.client_id == gone)) == 0, model.__name__
    for model in (Vehicle, VehiclePhoto, Listing):
        assert db.scalar(select(func.count()).select_from(model).where(model.client_id == keep)) > 0, model.__name__
    former = db.scalar(select(User).where(User.email.like("deleted-user-%")))
    assert former and not former.active and former.client_id is None and former.api_key_hash is None
    assert db.scalar(select(User).where(User.email == f"owner@{keep}.example")).client_id == keep
    assert db.scalar(select(CostEntry).where(CostEntry.note == "vendor")).client_id is None  # company cost history kept

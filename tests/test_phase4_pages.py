"""Every Phase 4 page renders (empty and populated) for the roles that may see it, with role-appropriate controls."""

from __future__ import annotations

from datetime import date

from tests.conftest import login, make_user
from tests.phase4 import issued_invoice, portal_login, two_clients

STAFF_PAGES = ["/clients", "/tickets", "/seo", "/client-reports"]
MANAGER_PAGES = ["/finance", "/finance/invoices", "/finance/expenses", "/finance/reconciliation", "/packages", "/costs"]


def _populate(client, t):
    a = t["alpha"].id
    inv = issued_invoice(client, t["h"]["admin"], a, 25000)
    cats = client.get("/api/v1/expense-categories", headers=t["h"]["admin"]).json()
    client.post("/api/v1/expenses", headers=t["h"]["admin"], json={"expense_date": str(date.today()), "vendor": "Hosting Co", "category_id": cats[0]["id"],
                                                                 "amount_cents": 4000, "business_purpose": "Hosting", "client_id": a})
    client.post("/api/v1/finance/bank-import", headers=t["h"]["admin"], json={"csv": f"date,description,amount\n{date.today()},Deposit,250.00\n"})
    client.post("/api/v1/tickets", headers=t["h"]["rep"], json={"client_id": a, "kind": "change_request", "title": "Change hours"})
    client.post("/api/v1/seo/sites", headers=t["h"]["rep"], json={"client_id": a, "base_url": "https://alpha.example"})
    rep = client.post("/api/v1/client-reports", headers=t["h"]["rep"], json={"client_id": a, "period_start": str(date.today().replace(day=1)),
                                                                            "period_end": str(date.today())}).json()
    client.post("/api/v1/costs/budgets", headers=t["h"]["admin"], json={"name": "AI", "category": "ai", "amount_cents": 1000})
    return inv, rep


def test_pages_render_for_admin(client, db, t_admin=None):
    t = two_clients(db)
    inv, rep = _populate(client, t)
    admin = make_user(db, "boss@p4.example", "admin")
    login(client, admin.email)
    for path in STAFF_PAGES + MANAGER_PAGES + [f"/clients/{t['alpha'].id}", f"/finance/invoices/{inv['id']}", f"/seo/clients/{t['alpha'].id}",
                                              f"/client-reports/{rep['id']}", f"/client-reports/{rep['id']}/download",
                                              "/finance/export/summary.csv", "/finance/export/invoices.csv"]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    page = client.get("/finance/invoices").text
    assert "New invoice" in page
    assert "Synthetic service" in client.get(f"/finance/invoices/{inv['id']}").text


def test_manager_sees_finance_without_edit_controls(client, db):
    t = two_clients(db)
    _populate(client, t)
    m = make_user(db, "mgr2@p4.example", "manager")
    login(client, m.email)
    for path in MANAGER_PAGES:
        assert client.get(path).status_code == 200, path
    assert "New invoice" not in client.get("/finance/invoices").text
    assert 'action="/packages/' not in client.get("/packages").text


def test_viewer_cannot_open_finance(client, db):
    v = make_user(db, "v2@p4.example", "viewer")
    login(client, v.email)
    for path in STAFF_PAGES:
        assert client.get(path).status_code == 200, path
    for path in MANAGER_PAGES:
        assert client.get(path).status_code == 403, path


def test_portal_renders_and_hides_staff_nav(client, db):
    t = two_clients(db)
    _populate(client, t)
    portal_login(client, "alpha@p4.example")
    page = client.get("/portal").text
    assert "Alpha Motors" in page and 'href="/prospects"' not in page and "SIMULATED providers active" not in page

"""Finance and usage-cost tracking (Phase 4, steps 1-2, 5). Synthetic records only."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from dealerflow.models import Client, CostEntry, User
from dealerflow.services import finance, usage
# Imported at module level on purpose: importing tests.conftest inside a test re-runs its module setup
# (environment reset, new template database) and points the app at a different database.
from tests.conftest import login
from tests.phase4 import issued_invoice, two_clients


@pytest.fixture
def t(db):
    return two_clients(db)


def admin(db) -> User:
    return db.query(User).filter_by(role="admin").first()


def test_unpaid_invoice_is_revenue_not_cash(client, t):
    h = t["h"]["admin"]
    inv = issued_invoice(client, h, t["alpha"].id, 50000)
    r = client.get("/api/v1/finance/report", headers=h).json()
    assert r["recorded_revenue_cents"] == 50000
    assert r["collected_net_cents"] == 0
    assert r["outstanding_receivables_cents"] == 50000
    pay = client.post("/api/v1/payments", headers=h, json={"client_id": t["alpha"].id, "invoice_id": inv["id"], "amount_cents": 20000, "received_date": str(date.today())})
    assert pay.status_code == 201
    r = client.get("/api/v1/finance/report", headers=h).json()
    assert (r["collected_net_cents"], r["outstanding_receivables_cents"]) == (20000, 30000)
    assert client.get(f"/api/v1/invoices?client_id={t['alpha'].id}", headers=h).json()[0]["display_status"] == "partially_paid"


def test_draft_invoice_is_not_revenue_and_pending_payment_not_cash(client, t):
    h = t["h"]["admin"]
    client.post("/api/v1/invoices", headers=h, json={"client_id": t["alpha"].id, "issue_date": str(date.today()), "due_date": str(date.today()),
                                                    "lines": [{"kind": "one_time", "description": "Proposal-ish", "unit_amount_cents": 99900}]})
    client.post("/api/v1/payments", headers=h, json={"client_id": t["alpha"].id, "amount_cents": 5000, "status": "pending"})
    r = client.get("/api/v1/finance/report", headers=h).json()
    assert r["recorded_revenue_cents"] == 0 and r["collected_net_cents"] == 0 and r["pending_payments_cents"] == 5000


def test_payment_rules(client, t):
    h = t["h"]["admin"]
    inv = issued_invoice(client, h, t["alpha"].id, 1000)
    base = {"client_id": t["alpha"].id, "invoice_id": inv["id"], "received_date": str(date.today())}
    assert client.post("/api/v1/payments", headers=h, json={**base, "amount_cents": 1500}).status_code == 400  # over balance
    assert client.post("/api/v1/payments", headers=h, json={**base, "client_id": t["bravo"].id, "amount_cents": 500}).status_code == 400  # wrong client
    assert client.post("/api/v1/payments", headers=h, json={**base, "received_date": None, "amount_cents": 500}).status_code == 400  # confirmed needs date
    future = str(date.today() + timedelta(days=5))
    assert client.post("/api/v1/payments", headers=h, json={**base, "received_date": future, "amount_cents": 500}).status_code == 400
    assert client.post("/api/v1/invoices/%d/actions" % inv["id"], headers=h, json={"action": "void"}).json()["status"] == "void"


def test_void_blocked_after_payment_and_credit_refund(client, t):
    h = t["h"]["admin"]
    cid = t["alpha"].id
    inv = issued_invoice(client, h, cid, 10000)
    client.post("/api/v1/payments", headers=h, json={"client_id": cid, "invoice_id": inv["id"], "amount_cents": 6000, "received_date": str(date.today())})
    assert client.post(f"/api/v1/invoices/{inv['id']}/actions", headers=h, json={"action": "void"}).status_code == 400
    assert client.post("/api/v1/credit-notes", headers=h, json={"client_id": cid, "invoice_id": inv["id"], "kind": "credit", "amount_cents": 5000,
                                                               "issued_date": str(date.today()), "reason": "too much"}).status_code == 400
    assert client.post("/api/v1/credit-notes", headers=h, json={"client_id": cid, "invoice_id": inv["id"], "kind": "credit", "amount_cents": 4000,
                                                               "issued_date": str(date.today()), "reason": "goodwill"}).status_code == 201
    assert client.post("/api/v1/credit-notes", headers=h, json={"client_id": cid, "invoice_id": inv["id"], "kind": "refund", "amount_cents": 1000,
                                                               "issued_date": str(date.today()), "reason": "partial refund"}).status_code == 201
    r = client.get("/api/v1/finance/report", headers=h).json()
    assert r["recorded_revenue_cents"] == 6000  # 10000 - 4000 credit
    assert r["collected_net_cents"] == 5000  # 6000 - 1000 refund
    assert r["outstanding_receivables_cents"] == 0
    assert client.get(f"/api/v1/invoices?client_id={cid}", headers=h).json()[0]["display_status"] == "paid"


def test_discount_lines_cannot_exceed_total(client, t):
    r = client.post("/api/v1/invoices", headers=t["h"]["admin"], json={"client_id": t["alpha"].id, "issue_date": "2026-10-01", "due_date": "2026-10-02",
                                                                     "lines": [{"kind": "one_time", "description": "a", "unit_amount_cents": 100},
                                                                               {"kind": "discount", "description": "d", "unit_amount_cents": -500}]})
    assert r.status_code == 400


def test_agreements_mrr_arr_invoicing_and_churn(client, db, t):
    h = t["h"]["admin"]
    pkgs = client.get("/api/v1/packages", headers=h).json()
    assert {p["code"] for p in pkgs} == {"listing_ops", "bilingual_lead_response", "dealership_growth"}
    assert all(p["pricing_status"] == "proposed" and p["recurring_fee_cents"] == 0 for p in pkgs)  # never hardcoded prices
    growth = next(p for p in pkgs if p["code"] == "dealership_growth")
    assert "Inventory imports" in growth["all_features"] and "SEO audits & reports" in growth["all_features"]
    r = client.patch(f"/api/v1/packages/{growth['id']}", headers=h, json={"recurring_fee_cents": 120000, "setup_fee_cents": 50000})
    assert r.status_code == 200
    start = date.today().replace(day=1) - timedelta(days=40)
    start = start.replace(day=1)
    a1 = client.post("/api/v1/agreements", headers=h, json={"client_id": t["alpha"].id, "package_id": growth["id"], "name": "Growth", "start_date": str(start),
                                                           "discount_percent": 10}).json()
    a2 = client.post("/api/v1/agreements", headers=h, json={"client_id": t["bravo"].id, "name": "Quarterly listing ops", "start_date": str(start),
                                                           "recurring_fee_cents": 90000, "billing_frequency": "quarterly"}).json()
    assert a1["setup_fee_cents"] == 50000 and a1["monthly_value_cents"] == 108000 and a2["monthly_value_cents"] == 30000
    for a in (a1, a2):
        client.post(f"/api/v1/agreements/{a['id']}/actions", headers=h, json={"action": "activate"})
    inv = client.post(f"/api/v1/agreements/{a1['id']}/actions", headers=h, json={"action": "invoice", "period_start": str(start)}).json()
    assert [li["kind"] for li in inv["lines"]] == ["setup", "recurring", "discount"] and inv["total_cents"] == 50000 + 120000 - 12000
    dup = client.post(f"/api/v1/agreements/{a1['id']}/actions", headers=h, json={"action": "invoice", "period_start": str(start)})
    assert dup.status_code == 400
    # Catalog price changes never rewrite the signed agreement.
    client.patch(f"/api/v1/packages/{growth['id']}", headers=h, json={"recurring_fee_cents": 1})
    r = client.get(f"/api/v1/finance/report?start={start}&end={date.today()}", headers=h).json()
    assert r["mrr_cents"] == 138000 and r["arr_cents"] == 138000 * 12 and r["active_clients"] == 2
    client.post(f"/api/v1/agreements/{a2['id']}/actions", headers=h, json={"action": "cancel", "effective_date": str(date.today()), "reason": "budget"})
    r = client.get(f"/api/v1/finance/report?start={start}&end={date.today()}", headers=h).json()
    assert r["mrr_cents"] == 108000 and r["clients_churned"] == 1 and r["churn_rate_percent"] == 50.0 and r["retention_rate_percent"] == 50.0


def test_acquisition_metrics_not_measurable_without_data(client, t):
    r = client.get("/api/v1/finance/report", headers=t["h"]["admin"]).json()
    assert r["cost_per_acquired_client_cents"] is None and r["cost_per_qualified_lead_cents"] is None and r["cost_per_booked_meeting_cents"] is None


def test_expense_categories_cac_and_margin(client, t):
    h = t["h"]["admin"]
    cats = {c["code"]: c for c in client.get("/api/v1/expense-categories", headers=h).json()}
    expected = {"ai_usage", "software", "hosting", "data_sources", "outreach", "advertising", "domains", "contractors", "telephone", "payment_processing", "other"}
    assert expected <= set(cats)
    today = str(date.today())
    client.post("/api/v1/expenses", headers=h, json={"expense_date": today, "vendor": "Ads Inc", "category_id": cats["advertising"]["id"], "amount_cents": 30000, "business_purpose": "Ads"})
    client.post("/api/v1/expenses", headers=h, json={"expense_date": today, "vendor": "Host", "category_id": cats["hosting"]["id"], "amount_cents": 2000,
                                                    "business_purpose": "Alpha site", "client_id": t["alpha"].id})
    client.post("/api/v1/agreements", headers=h, json={"client_id": t["alpha"].id, "name": "x", "start_date": today, "recurring_fee_cents": 10000})
    a = client.get("/api/v1/agreements", headers=h)  # noqa: F841 (no list endpoint needed)
    issued_invoice(client, h, t["alpha"].id, 10000)
    r = client.get("/api/v1/finance/report", headers=h).json()
    assert r["expenses_cents"] == 32000 and r["acquisition_spend_cents"] == 30000
    assert r["operating_result_accrual_cents"] == 10000 - 32000 and r["operating_result_cash_cents"] == -32000
    alpha = next(c for c in r["per_client"] if c["client_id"] == t["alpha"].id)
    assert alpha["direct_expense_cents"] == 2000 and alpha["gross_margin_percent"] == 80.0
    assert "not tax" in r["tax_disclaimer"]
    bad = client.post("/api/v1/expenses", headers=h, json={"expense_date": today, "vendor": "X", "category_id": cats["other"]["id"], "amount_cents": 100,
                                                          "business_purpose": "y", "notes": "paid with 4111 1111 1111 1111"})
    assert bad.status_code == 400


def test_csv_exports(client, t):
    h = t["h"]["admin"]
    issued_invoice(client, h, t["alpha"].id, 1234)
    for kind in ("invoices", "payments", "expenses", "summary"):
        r = client.get(f"/api/v1/finance/export/{kind}.csv", headers=h)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "12.34" in client.get("/api/v1/finance/export/invoices.csv", headers=h).text
    assert client.get("/api/v1/finance/export/secrets.csv", headers=h).status_code == 400


def test_reconciliation_import_duplicates_and_match(client, t):
    h = t["h"]["admin"]
    inv = issued_invoice(client, h, t["alpha"].id, 25000)
    client.post("/api/v1/payments", headers=h, json={"client_id": t["alpha"].id, "invoice_id": inv["id"], "amount_cents": 25000, "received_date": str(date.today())})
    csv = f"date,description,amount\n{date.today()},ACH ALPHA,250.00\n{date.today()},ACH ALPHA,250.00\nbad,row,x\n"
    r = client.post("/api/v1/finance/bank-import", headers=h, json={"csv": csv}).json()
    assert r["created"] == 2 and len(r["errors"]) == 1  # two identical lines are both real
    assert client.post("/api/v1/finance/bank-import", headers=h, json={"csv": csv}).json()["duplicates"] == 2
    txs = client.get("/api/v1/finance/reconciliation", headers=h).json()
    sugg = txs[0]["suggestions"][0]
    assert sugg["type"] == "payment"
    assert client.post(f"/api/v1/finance/reconciliation/{txs[0]['id']}", headers=h, json={"match_type": "payment", "match_id": sugg["id"]}).json()["status"] == "matched"
    assert client.post(f"/api/v1/finance/reconciliation/{txs[1]['id']}", headers=h, json={"match_type": "payment", "match_id": sugg["id"] + 99}).status_code == 400


def test_break_even_is_labelled_hypothetical(client, t):
    r = client.post("/api/v1/finance/break-even", headers=t["h"]["manager"], json={"fixed_monthly_costs_cents": 300000, "price_per_client_cents": 100000,
                                                                                 "variable_cost_per_client_cents": 25000}).json()
    assert r["basis"] == "hypothetical_scenario" and r["clients_needed_to_break_even"] == 4
    r = client.post("/api/v1/finance/break-even", headers=t["h"]["manager"], json={"fixed_monthly_costs_cents": 1, "price_per_client_cents": 10,
                                                                                 "variable_cost_per_client_cents": 10}).json()
    assert r["clients_needed_to_break_even"] is None


def test_cash_flow_separates_actuals_and_forecast(db, t):
    rows = finance.cash_flow_trend(db, date.today().replace(day=1), date.today())
    assert all(m["basis"] == "actual" for m in rows["actuals"]) and all(m["basis"] == "forecast" for m in rows["forecast"])
    assert rows["forecast_assumptions"]


def test_stripe_invoice_paid_records_confirmed_payment_once(client, db, t, monkeypatch):
    from pydantic import SecretStr

    from dealerflow.config import get_settings
    from dealerflow.integrations.stripe import sign
    from dealerflow.jobs import run_pending
    from dealerflow.models import Invoice, Payment

    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", SecretStr("whsec_t"))
    inv = issued_invoice(client, t["h"]["admin"], t["alpha"].id, 30000)
    db.get(Invoice, inv["id"]).external_id = "in_test_123"
    db.commit()
    body = json.dumps({"id": "evt_paid", "type": "invoice.paid", "created": 1760000000,
                       "data": {"object": {"id": "in_test_123", "amount_paid": 30000, "currency": "usd", "status_transitions": {"paid_at": 1760000000},
                                           "payment_method_details": {"card": {"last4": "4242"}}}}}).encode()
    for _ in range(2):
        client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign(body, "whsec_t")})
    run_pending()
    db.expire_all()
    pays = db.query(Payment).all()
    assert len(pays) == 1 and pays[0].source == "stripe" and pays[0].status == "confirmed" and pays[0].invoice_id == inv["id"]
    from dealerflow.models import WebhookEvent

    stored = db.query(WebhookEvent).one().payload
    assert "payment_method_details" not in json.dumps(stored)  # card details are never stored


def test_usage_bases_idempotency_and_billing_import(db, t):
    e = usage.record_usage(db, provider="anthropic:claude-opus-5-5", category="ai", metric="input_tokens", quantity=1_000_000, ref="r1",
                           client_id=t["alpha"].id)
    assert e.basis == "estimated" and float(e.amount) == pytest.approx(4.0)
    assert usage.record_usage(db, provider="anthropic:claude-opus-5-5", category="ai", metric="input_tokens", quantity=5, ref="r1").id == e.id
    u = usage.record_usage(db, provider="newvendor", category="data_api", metric="lookups", quantity=10, ref="r2")
    assert u.basis == "unknown" and float(u.amount) == 0
    res = usage.import_billing_csv(db, "date,metric,quantity,cost\n2026-10-01,input_tokens,100,0.42\nbad,x,1,1\n", "anthropic:claude-opus-5-5", "ai")
    assert res["created"] == 1 and len(res["errors"]) == 1
    assert usage.import_billing_csv(db, "date,metric,quantity,cost\n2026-10-01,input_tokens,100,0.42\n", "anthropic:claude-opus-5-5", "ai")["created"] == 0
    db.commit()
    s = usage.summary(db, date(2026, 1, 1), date.today() + timedelta(days=1))
    assert s["totals"]["verified_billing"] == pytest.approx(0.42) and s["totals"]["estimated"] == pytest.approx(4.0) and s["totals"]["unknown_records"] == 1
    alpha_only = usage.summary(db, date(2026, 1, 1), date.today() + timedelta(days=1), client_id=t["alpha"].id)
    assert alpha_only["totals"]["estimated"] == pytest.approx(4.0) and alpha_only["totals"]["verified_billing"] == 0


def test_budget_threshold_exceeded_and_spike_alerts(db, t):
    from dealerflow.models import Alert, Budget

    db.add(Budget(name="AI monthly", category="ai", period="monthly", amount_cents=1000, alert_threshold_percent=80))
    from dealerflow.db import utcnow

    now = utcnow()
    for d in range(1, 8):  # quiet week
        db.add(CostEntry(incurred_on=now - timedelta(days=d), category="ai", amount=0.01, is_estimate=True))
    db.add(CostEntry(incurred_on=now, category="ai", amount=8.5, is_estimate=True))
    db.commit()
    raised = usage.check_budgets(db, now)
    assert any(r.endswith(":80") for r in raised) and any(r.endswith(":spike") for r in raised)
    db.add(CostEntry(incurred_on=now, category="ai", amount=5, is_estimate=True))
    db.commit()
    assert any(r.endswith(":100") for r in usage.check_budgets(db, now))
    usage.check_budgets(db, now)
    db.commit()
    kinds = sorted(a.kind for a in db.query(Alert).all())
    assert kinds == ["budget_exceeded", "budget_threshold", "usage_spike"]  # deduplicated


def test_ai_gateway_is_off_by_default_and_records_tokens_when_on(db, monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr

    from dealerflow.config import get_settings
    from dealerflow.integrations import llm

    with pytest.raises(llm.AIUnavailable):
        llm.complete(db, system="s", prompt="p", feature="test")
    monkeypatch.setattr(get_settings(), "ai_enabled", True)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr("sk-ant-test"))
    fake = SimpleNamespace(id="msg_1", model="claude-opus-5-5", stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0),
                           content=[SimpleNamespace(type="text", text="hello")])

    class FakeClient:
        def __init__(self, **kw):
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: fake))

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    res = llm.complete(db, system="s", prompt="p", feature="test")
    assert res.text == "hello" and res.estimated_cost == pytest.approx(1000 * 0.000004 + 500 * 0.00002)
    rows = db.query(CostEntry).filter(CostEntry.provider == "anthropic:claude-opus-5-5").all()
    assert {r.metric for r in rows} == {"input_tokens", "output_tokens"} and all(r.basis == "estimated" for r in rows)


def test_finance_ui_forms(client, db, t):
    a = db.query(User).filter_by(role="admin").first()
    csrf = login(client, a.email)
    r = client.post("/finance/invoices", data={"csrf": csrf, "client_id": str(t["alpha"].id), "issue_date": "2026-10-01", "due_date": "2026-10-15",
                                               "kind": "setup", "description": "Setup", "amount": "1,500.00"}, follow_redirects=False)
    assert r.status_code == 303
    iid = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/finance/invoices/{iid}/issue", data={"csrf": csrf})
    client.post(f"/finance/invoices/{iid}/payment", data={"csrf": csrf, "amount": "500.005", "received_date": "2026-10-02", "method": "ach"})
    client.post(f"/finance/invoices/{iid}/payment", data={"csrf": csrf, "amount": "500", "received_date": "2026-10-02", "method": "check"})
    db.expire_all()
    from dealerflow.models import Invoice

    inv = db.get(Invoice, iid)
    assert finance.invoice_paid(inv) == 50000 and finance.invoice_balance(inv) == 100000  # 3-decimal amount was rejected
    assert db.get(Client, t["alpha"].id) is not None

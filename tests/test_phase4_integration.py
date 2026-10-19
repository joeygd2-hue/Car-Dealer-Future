"""Regression tests for the seams between Phase 4 and the rest of dealerflow (one Client, one client-user
model, one AI gateway, Phase 2 tables behind Phase 4 reports)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from dealerflow.config import get_settings
from dealerflow.db import session_factory, utcnow
from dealerflow.models import Client, Conversation, CostEntry, User
from tests.conftest import login, make_user
from tests.dealer_helpers import dealer_login, import_csv, make_client, page_csrf, vehicle_by_stock


def test_a_conversation_cannot_point_at_another_dealerships_vehicle(db):
    a, b = make_client("Alpha Auto"), make_client("Bravo Motors")
    cb, _, _ = import_csv(b)
    vb = vehicle_by_stock(cb, "B200").id
    cb.close()
    s = session_factory()()
    s.add(Conversation(client_id=a, channel="web_form", vehicle_id=vb, simulated=True))
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()
    s.add(Conversation(client_id=b, channel="web_form", vehicle_id=vb, simulated=True))
    s.flush()  # the same vehicle is fine for its own dealership
    s.rollback()
    s.close()


def test_customer_acquisition_cost_ignores_client_service_costs(db):
    from dealerflow.services import reports

    cid = make_client("Alpha Auto")
    now = utcnow() - timedelta(hours=1)
    db.add_all([CostEntry(incurred_on=now, category="data_api", amount=12.0, is_estimate=False),
                CostEntry(incurred_on=now, category="ai", amount=99.0, is_estimate=False, client_id=cid, provider="anthropic:x")])
    db.flush()
    w = reports.weekly(db)
    assert w["spend_actual"] == 12.0  # delivery costs for a client are not acquisition spend


def test_listing_ai_needs_the_shared_ai_master_switch(monkeypatch):
    monkeypatch.setenv("DF_LISTING_AI_ENABLED", "true")
    monkeypatch.setenv("DF_ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    assert get_settings().listing_ai_available is False
    monkeypatch.setenv("DF_AI_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().listing_ai_available is True
    get_settings.cache_clear()


class _Copy(BaseModel):
    title: str


def _fake_anthropic(monkeypatch, stop_reason: str):
    import anthropic

    resp = SimpleNamespace(id="msg_s1", model="claude-opus-5-5", stop_reason=stop_reason, parsed_output=_Copy(title="t"),
                           usage=SimpleNamespace(input_tokens=200, output_tokens=100, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0))

    class FakeClient:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(parse=lambda **k: resp)

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    monkeypatch.setattr(get_settings(), "ai_enabled", True)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr("sk-ant-test"))


def test_structured_ai_calls_record_usage_against_the_client(db, monkeypatch):
    from dealerflow.integrations import llm
    from dealerflow.integrations.errors import NotConfiguredError

    with pytest.raises(NotConfiguredError):  # off by default
        llm.structured_call("s", "u", _Copy)
    cid = make_client("Alpha Auto")
    _fake_anthropic(monkeypatch, "end_turn")
    res = llm.structured_call("s", "u", _Copy, db=db, feature="listing_generate", client_id=cid)
    assert res.parsed.title == "t"
    rows = db.scalars(select(CostEntry).where(CostEntry.client_id == cid)).all()
    assert {r.metric for r in rows} == {"input_tokens", "output_tokens"} and {r.category for r in rows} == {"ai"}


def test_a_refused_structured_call_is_permanent(db, monkeypatch):
    from dealerflow.integrations import llm

    _fake_anthropic(monkeypatch, "refusal")
    with pytest.raises(llm.Refused):
        llm.structured_call("s", "u", _Copy, db=db)


def test_staff_created_dealership_user_gets_its_role_in_the_workspace(client, db):
    from tests.dealer_helpers import PASSWORD

    make_user(db, "admin@agency.example", role="admin")
    c = Client(name="Alpha Auto", status="active")
    db.add(c)
    db.commit()
    csrf = login(client, "admin@agency.example")
    r = client.post(f"/clients/{c.id}/users", data={"csrf": csrf, "email": "Viewer@Alpha.example", "password": PASSWORD,
                                                    "client_role": "dealer_viewer"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/clients/{c.id}/users", data={"csrf": csrf, "email": "x@alpha.example", "password": PASSWORD,
                                                    "client_role": "admin"}, follow_redirects=False)
    db.expire_all()
    assert db.scalar(select(User).where(User.email == "x@alpha.example")) is None  # staff roles cannot be granted here
    u = db.scalar(select(User).where(User.email == "viewer@alpha.example"))
    assert (u.role, u.client_id, u.client_role) == ("client", c.id, "dealer_viewer")

    from fastapi.testclient import TestClient

    from dealerflow.web.app import create_app

    dealer = TestClient(create_app())
    dealer_login(dealer, "viewer@alpha.example")
    assert dealer.get("/portal").status_code == 200 and dealer.get(f"/dealers/{c.id}").status_code == 200
    csrf = page_csrf(dealer, f"/dealers/{c.id}")
    r = dealer.post(f"/dealers/{c.id}/imports", data={"csrf": csrf}, files={"file": ("x.csv", b"stock\nA1\n", "text/csv")})
    assert r.status_code == 403  # a dealer viewer cannot import
    assert dealer.get("/clients").status_code == 403


def test_simulated_prospect_becomes_a_simulated_client(db):
    from dealerflow.models import Prospect
    from dealerflow.services import tenancy

    p = Prospect(name="Sim Motors", normalized_name="sim motors", stage="won", simulated=True)
    db.add(p)
    db.flush()
    c = tenancy.create_client(db, name=p.name, actor=None, prospect=p)
    assert c.simulated is True and c.status == "onboarding"


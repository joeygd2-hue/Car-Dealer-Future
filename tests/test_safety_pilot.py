"""Stage 4: pilot mode limits every LIVE send to internal test recipients; every send path writes the ledger;
re-approving a retried outreach email really sends it; the pilot console is admin-only."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from dealerflow.config import get_settings
from dealerflow.integrations import email as email_mod
from tests.conftest import csrf_of, login, make_user
from tests.customer_helpers import chat, dealer_login, run_worker, world  # noqa: F401
from tests.test_compliance import _prospect, _tpl


class FakeReal(email_mod.MockEmailProvider):
    name, simulated = "fake_real", False


@pytest.fixture
def live_outreach(monkeypatch, db, admin):
    """A 'real' email provider (records instead of sending) with outreach live operation fully authorized."""
    from dealerflow.services import agent

    FakeReal.outbox = []
    monkeypatch.setenv("DF_OUTREACH_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr("dealerflow.services.outreach.get_email_provider", lambda: FakeReal())
    agent.authorize_live(db, actor=admin, authorize=True)
    db.commit()
    from dealerflow import handlers  # noqa: F401


def _real_prospect(db, n: int):
    """Non-simulated synthetic prospect (FakeReal never sends anything; it only records)."""
    from dealerflow.services.prospects import LeadRecord, intake

    rec = LeadRecord(name=f"Pilot Test Dealer {n}", source="manual", simulated=False, city="Sugar Land", state="TX",
                     website=f"https://pilot-dealer{n}-test.com", email=f"owner@pilot-dealer{n}-test.com", dealership_type="independent_used")
    return intake(db, rec).prospect


def _approve_and_run(db, admin, p):
    from dealerflow.jobs import run_pending
    from dealerflow.models import OutreachMessage
    from dealerflow.services import outreach

    m = outreach.create_draft(db, p, _tpl(db), actor=admin)
    outreach.approve(db, m, actor=admin)
    db.commit()
    run_pending()
    db.expire_all()
    return db.get(OutreachMessage, m.id)


def test_live_outreach_under_pilot_mode_only_reaches_allowlisted_inboxes(db, admin, live_outreach):
    from dealerflow.models_customer import SendAttempt
    from dealerflow.services import pilot

    blocked = _approve_and_run(db, admin, _real_prospect(db, 1))
    assert blocked.status == "cancelled" and "pilot mode" in blocked.error and FakeReal.outbox == []
    pilot.add(db, "EMAIL", "owner@pilot-dealer2-test.com", label="internal tester", actor_id=admin.id)
    db.commit()
    sent = _approve_and_run(db, admin, _real_prospect(db, 2))
    assert sent.status == "sent", sent.error
    assert [m.to for m, _ in FakeReal.outbox] == ["owner@pilot-dealer2-test.com"]
    rows = {a.ref_id: a for a in db.scalars(select(SendAttempt).where(SendAttempt.kind == "OUTREACH_EMAIL"))}
    assert rows[blocked.id].status == "BLOCKED" and rows[sent.id].status == "SENT" and rows[sent.id].live


def test_pilot_mode_off_lets_live_outreach_through_existing_gates(db, admin, live_outreach, monkeypatch):
    monkeypatch.setenv("DF_PILOT_MODE", "false")
    get_settings.cache_clear()
    m = _approve_and_run(db, admin, _real_prospect(db, 3))
    assert m.status == "sent", m.error


def test_simulated_outreach_is_recorded_in_the_ledger_and_not_affected_by_pilot(db, admin):
    from dealerflow.models_customer import SendAttempt

    m = _approve_and_run(db, admin, _prospect(db, 4))
    a = db.scalar(select(SendAttempt).where(SendAttempt.kind == "OUTREACH_EMAIL", SendAttempt.ref_id == m.id))
    assert m.status == "sent" and m.simulated and a.status == "SIMULATED" and not a.live


def test_reapproving_a_retried_outreach_email_sends_it(db, admin, client):
    """Phase 1 bug: the second approval reused the finished job key, so the message was never sent."""
    from dealerflow.integrations.email import MockEmailProvider
    from dealerflow.integrations.errors import IntegrationError
    from dealerflow.jobs import run_pending
    from dealerflow.models import OutreachMessage
    from dealerflow.services import outreach

    MockEmailProvider.fail_next = [IntegrationError("provider 500 after accept")]
    m = _approve_and_run(db, admin, _prospect(db, 5))
    assert m.status == "failed" and MockEmailProvider.outbox == []
    m.status, m.error = "draft", None  # what the Retry button does
    outreach.approve(db, m, actor=admin)
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(OutreachMessage, m.id).status == "sent" and len(MockEmailProvider.outbox) == 1


def test_live_website_chat_reply_under_pilot_mode_needs_an_allowlisted_visitor(world, db, monkeypatch):
    from dealerflow.models_customer import CustomerConversation, CustomerMessage, SendAttempt
    from dealerflow.models_ops import IntegrationConnection
    from dealerflow.services import pilot
    from dealerflow.services.customer.outbound import queue_outbound

    monkeypatch.setenv("DF_LIVE_CUSTOMER_MESSAGING_ENABLED", "true")
    get_settings.cache_clear()
    a = world["a"]
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == a["id"], IntegrationConnection.provider == "website_chat"))
    conn.status = "connected"
    db.commit()
    for visitor in ("stranger-0001", "tester-00001"):
        _, conv_id, _ = chat(a["id"], visitor, "Hi, are you open Saturday?")
        conv = db.get(CustomerConversation, conv_id)
        queue_outbound(db, conv, "Thanks for reaching out to Alpha Auto (TEST).", sender_type="STAFF", user_id=a["reviewer"])
        db.commit()
        if visitor == "stranger-0001":
            run_worker()
            pilot.add(db, "CHAT_VISITOR", "tester-00001", label="internal tester", actor_id=a["admin"])
            db.commit()
    run_worker()
    db.expire_all()
    outs = {m.conversation_id: m for m in db.scalars(select(CustomerMessage).where(CustomerMessage.direction == "OUTBOUND"))}
    statuses = sorted(m.delivery_status for m in outs.values())
    assert statuses == ["BLOCKED", "SENT"], statuses
    assert db.scalar(select(SendAttempt).where(SendAttempt.status == "BLOCKED", SendAttempt.live.is_(True))) is not None


def test_pilot_console_is_admin_only_and_return_to_sandbox_withdraws_live_authorizations(client, db, world):
    from dealerflow.models import User
    from dealerflow.models_ops import IntegrationConnection
    from dealerflow.services import agent

    admin = db.get(User, world["staff_admin"])
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == world["a"]["id"]))
    conn.status = "connected"
    agent.authorize_live(db, actor=admin, authorize=True)
    db.commit()
    login(client, "manager@agency.example")
    assert client.get("/admin/pilot").status_code == 403
    client.cookies.clear()
    dealer_login(client, "admin@a.example")
    assert client.get("/admin/pilot").status_code == 403
    client.cookies.clear()
    login(client, "admin@agency.example")
    page = client.get("/admin/pilot")
    assert page.status_code == 200 and "Send ledger" in page.text and "all " in page.text  # audit integrity summary
    r = client.post("/admin/pilot/return-to-sandbox", data={"reason": "end of pilot", "csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(IntegrationConnection, conn.id).status == "sandbox" and not agent.state(db).live_authorized
    client.post("/admin/pilot/emergency-stop", data={"reason": "drill", "csrf": csrf_of(client)})
    db.expire_all()
    assert agent.state(db).status == "emergency_stopped"


def test_client_report_email_under_pilot_needs_an_allowlisted_inbox(db, monkeypatch):
    from dealerflow.models_dealer import Client
    from dealerflow.models_ops import IntegrationConnection
    from dealerflow.services import client_email, pilot

    sent = []

    class Recorder(email_mod.MockEmailProvider):
        name, simulated = "recorder", False

        def send(self, msg):
            sent.append(msg.to)
            return super().send(msg)

    monkeypatch.setenv("DF_CLIENT_EMAIL_SENDING_ENABLED", "true")
    monkeypatch.setenv("DF_EMAIL_PROVIDER", "smtp")
    get_settings.cache_clear()
    monkeypatch.setattr(client_email, "get_email_provider", lambda: Recorder())
    c = Client(name="Report Co (TEST)", slug="report-co")
    db.add(c)
    db.flush()
    db.add(IntegrationConnection(client_id=c.id, kind="report_email", provider="smtp", status="connected"))
    db.commit()
    assert client_email.send(db, c, to="owner@report.example", subject="s", body="b", key="k1").startswith("not_sent: pilot mode")
    admin = make_user(db, "admin2@test.example", "admin")
    pilot.add(db, "EMAIL", "owner@report.example", label="internal tester", actor_id=admin.id)
    assert client_email.send(db, c, to="owner@report.example", subject="s", body="b", key="k2") == "sent"
    assert sent == ["owner@report.example"]

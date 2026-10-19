"""Pilot-mode guarantees for customer messaging (adapted from Phase 3's pilot tests).

In the unified app only first-party website chat can be live: it needs DF_CUSTOMER_MESSAGING_LIVE_ENABLED *and* the
client's ``customer_messaging`` WEBSITE_CHAT connection in status ``connected``. "Live" here means the reply is shown
to the visitor in the dealership's own chat widget; nothing is sent to an outside provider. Meta Messenger and Twilio
live adapters were not ported, so those channels are always SIMULATED (covered at the end).
"""

import pytest
from sqlalchemy import select

from dealerflow.config import get_settings
from dealerflow.models import AgentState, Conversation, IntegrationConnection, Message, PilotRecipient, SandboxRecord, SendAttempt
from dealerflow.services.conversations.outbound import queue_outbound
from tests.conv_helpers import chat, login, run_worker


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setattr(get_settings(), "customer_messaging_live_enabled", True)
    yield


def _connect_chat(db, world, status="connected"):
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == world["a"]["d"].id,
                                                         IntegrationConnection.kind == "customer_messaging",
                                                         IntegrationConnection.provider == "WEBSITE_CHAT"))
    conn.status = status
    db.commit()


def _conv(db, world, visitor):
    _, cid, _ = chat(world["a"]["d"], visitor, "Is the Civic available?")
    run_worker()
    return db.get(Conversation, cid)


def _send(db, world, conv, body):
    msg = queue_outbound(db, conv, body, sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    return db.get(Message, msg.id)


def _attempt(db, msg_id):
    return db.scalar(select(SendAttempt).where(SendAttempt.kind == "CUSTOMER_MESSAGE", SendAttempt.ref_id == msg_id))


def _visible(client, world, visitor):
    d = world["a"]["d"]
    return [x["text"] for x in client.get(f"/api/chat/{d.public_chat_key}/messages", params={"visitor_id": visitor}).json()["messages"]]


def test_simulated_by_default_and_visitor_sees_nothing(db, world, client):
    conv = _conv(db, world, "visitor-pilot-0")
    m = _send(db, world, conv, "hi")
    assert m.delivery_status == "SIMULATED" and not _attempt(db, m.id).live
    assert _visible(client, world, "visitor-pilot-0") == []


def test_env_flag_alone_is_not_enough(db, world, client, live_env):
    conv = _conv(db, world, "visitor-pilot-1")
    m = _send(db, world, conv, "hi")  # connection is only 'sandbox'
    assert m.delivery_status == "SIMULATED" and _visible(client, world, "visitor-pilot-1") == []


def test_connection_alone_is_not_enough(db, world, client):
    _connect_chat(db, world)
    conv = _conv(db, world, "visitor-pilot-2")
    m = _send(db, world, conv, "hi")  # DF_CUSTOMER_MESSAGING_LIVE_ENABLED is off
    assert m.delivery_status == "SIMULATED" and _visible(client, world, "visitor-pilot-2") == []


def test_live_chat_blocked_unless_visitor_allowlisted(db, world, client, live_env):
    _connect_chat(db, world)
    conv = _conv(db, world, "visitor-pilot-3")
    m = _send(db, world, conv, "hi again")
    att = _attempt(db, m.id)
    assert m.delivery_status == "BLOCKED" and att.live and att.status == "BLOCKED" and "allowlist" in att.reasons[0]
    assert _visible(client, world, "visitor-pilot-3") == []
    db.add(PilotRecipient(kind="CHAT_VISITOR", value="visitor-pilot-3", label="QA browser"))
    db.commit()
    m2 = _send(db, world, conv, "allowlisted hello")
    assert m2.delivery_status == "SENT" and _attempt(db, m2.id).status == "SENT"
    assert _visible(client, world, "visitor-pilot-3") == ["allowlisted hello"]
    assert db.scalar(select(SandboxRecord).where(SandboxRecord.kind == "CHANNEL_OUT")) is None  # first-party: no provider


def test_emergency_stop_blocks_allowlisted_live_send(db, world, client, live_env):
    _connect_chat(db, world)
    db.add(PilotRecipient(kind="CHAT_VISITOR", value="visitor-pilot-4", label="QA browser"))
    conv = _conv(db, world, "visitor-pilot-4")
    st = db.get(AgentState, 1)
    st.status = "emergency_stopped"
    db.commit()
    m = _send(db, world, conv, "hi")
    assert m.delivery_status == "BLOCKED" and "emergency stop" in _attempt(db, m.id).reasons[0]
    assert _visible(client, world, "visitor-pilot-4") == []


def test_messenger_and_sms_never_live(db, world, live_env):
    for channel in ("META_MESSENGER", "TWILIO_SMS"):
        db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider=channel, status="connected",
                                     authorization_note="test only"))
        conv = _conv(db, world, f"visitor-{channel}")
        conv.channel, conv.external_conversation_id = channel, "+18325550000" if channel == "TWILIO_SMS" else "PSID_X"
        db.add(PilotRecipient(kind="PHONE" if channel == "TWILIO_SMS" else "META_PSID",
                              value="8325550000" if channel == "TWILIO_SMS" else "PSID_X", label="ops"))
        db.commit()
        m = _send(db, world, conv, "hello")
        assert m.delivery_status == "SIMULATED" and not _attempt(db, m.id).live


def test_pilot_console_admin_only(client, db, world):
    db.add(PilotRecipient(kind="CHAT_VISITOR", value="visitor-ops", label="Ops test browser"))
    db.commit()
    csrf = login(client, "ops@platform.example")
    page = client.get("/conversations/pilot")
    assert page.status_code == 200 and "visitor-ops" in page.text
    r = client.post("/conversations/pilot/recipients", data={"kind": "EMAIL", "value": "QA@Agency.example", "label": "qa",
                                                             "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert db.scalar(select(PilotRecipient).where(PilotRecipient.value == "qa@agency.example")) is not None
    assert client.get("/api/v1/conversations/pilot/ledger").status_code == 200
    client.cookies.clear()
    staff_csrf = login(client, "staff@a.example")
    assert client.get("/conversations/pilot").status_code == 403
    assert client.post("/conversations/pilot/recipients", data={"kind": "EMAIL", "value": "x@y.z", "label": "nope",
                                                                "csrf": staff_csrf}).status_code == 403

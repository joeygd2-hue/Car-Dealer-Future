"""Pilot mode guarantees, exercised through the live code path with the provider HTTP endpoint mocked
(no real provider is contacted)."""

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import utcnow
from app.integrations import meta_messenger
from app.models import Conversation, IntegrationConnection, LiveAuthorization, Message, PilotRecipient, SendAttempt
from app.security.crypto import encrypt_json
from app.services import controls, send_gate
from app.services.inbox.outbound import queue_outbound
from tests.conftest import chat, login, run_worker

CALLS = []


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("LIVE_INTEGRATIONS_ENABLED", "true")
    get_settings.cache_clear()
    CALLS.clear()

    def fake_init(self, page_id, page_access_token, graph_version="v23.0", client=None):
        self.page_id, self.token = page_id, page_access_token
        self.url = f"https://graph.facebook.com/{graph_version}/{page_id}/messages"

        def handler(req):
            CALLS.append(req)
            return httpx.Response(200, json={"message_id": f"m.{len(CALLS)}"})

        self.client = httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(meta_messenger.MessengerChannel, "__init__", fake_init)
    yield
    get_settings.cache_clear()


def _messenger_conv(db, world, psid):
    d = world["a"]["d"]
    if not db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "META_MESSENGER")):
        db.add(IntegrationConnection(dealership_id=d.id, provider="META_MESSENGER", external_account_id="PAGE_A",
                                     encrypted_credentials=encrypt_json({"page_access_token": "tok"})))
        controls.authorize_live(db, "customer_messaging.META_MESSENGER", user_id=world["platform"].id,
                                statement="pilot test authorization for internal accounts", dealership_id=d.id)
    db.commit()
    _, cid, _ = chat(d, f"tmp-{psid}", "Is the Civic available?")
    run_worker()
    conv = db.get(Conversation, cid)
    conv.channel, conv.external_conversation_id, conv.last_inbound_at = "META_MESSENGER", psid, utcnow()
    db.commit()
    return conv


def _attempt(db, msg_id):
    return db.scalar(select(SendAttempt).where(SendAttempt.kind == "CUSTOMER_MESSAGE", SendAttempt.ref_id == msg_id))


def test_live_send_blocked_unless_recipient_allowlisted(db, world, live_env):
    conv = _messenger_conv(db, world, "PSID_REAL_CUSTOMER")
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "BLOCKED" and CALLS == []
    att = _attempt(db, msg.id)
    assert att.live and att.status == "BLOCKED" and "allowlist" in att.reasons[0]


def test_live_send_to_allowlisted_internal_recipient(db, world, live_env):
    db.add(PilotRecipient(kind="META_PSID", value="PSID_OPS_TEST", label="Ops test account"))
    db.commit()
    conv = _messenger_conv(db, world, "PSID_OPS_TEST")
    msg = queue_outbound(db, conv, "Pilot test message", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    m = db.get(Message, msg.id)
    assert m.delivery_status == "SENT" and m.provider_message_id == "m.1" and len(CALLS) == 1
    assert _attempt(db, msg.id).status == "SENT"


def test_no_live_authorization_means_simulated_even_with_env_on(db, world, live_env):
    db.add(PilotRecipient(kind="META_PSID", value="PSID_OPS_TEST", label="Ops test account"))
    conv = _messenger_conv(db, world, "PSID_OPS_TEST")
    for a in db.scalars(select(LiveAuthorization)):
        a.revoked_at = utcnow()
    db.commit()
    msg = queue_outbound(db, conv, "hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SIMULATED" and CALLS == []


def test_emergency_stop_blocks_allowlisted_live_send(db, world, live_env):
    db.add(PilotRecipient(kind="META_PSID", value="PSID_OPS_TEST", label="Ops test account"))
    conv = _messenger_conv(db, world, "PSID_OPS_TEST")
    controls.set_flag(db, send_gate.EMERGENCY_STOP_ALL, True, user_id=None, reason="drill")
    db.commit()
    msg = queue_outbound(db, conv, "hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "BLOCKED" and CALLS == []


def test_pilot_console_controls(client, db, world, live_env):
    db.add(PilotRecipient(kind="META_PSID", value="PSID_OPS_TEST", label="Ops test account"))
    _messenger_conv(db, world, "PSID_OPS_TEST")
    csrf = login(client, "ops@platform.example")
    page = client.get("/admin/pilot")
    assert page.status_code == 200 and "Send ledger" in page.text and "PSID_OPS_TEST" in page.text
    # Emergency stop on → banner visible everywhere.
    client.post("/admin/pilot/emergency-stop", data={"on": 1, "reason": "drill", "csrf_token": csrf})
    assert "EMERGENCY STOP ACTIVE" in client.get("/admin").text
    assert "err=" in client.post("/admin/pilot/emergency-stop", data={"on": 0, "reason": "", "csrf_token": csrf},
                                 follow_redirects=False).headers["location"]
    client.post("/admin/pilot/emergency-stop", data={"on": 0, "reason": "drill complete", "csrf_token": csrf})
    # Return to sandbox revokes every live authorization.
    client.post("/admin/pilot/return-to-sandbox", data={"reason": "end of test", "csrf_token": csrf})
    db.expire_all()
    assert db.scalar(select(LiveAuthorization).where(LiveAuthorization.revoked_at.is_(None))) is None
    # Allowlist management is admin-only.
    staff_client_csrf = login(client, "staff@a.example")
    assert client.post("/admin/pilot/allowlist", data={"kind": "EMAIL", "value": "x@y.z", "label": "nope",
                                                       "csrf_token": staff_client_csrf}).status_code == 403


def test_website_chat_requires_authorization_and_allowlist(db, world, live_env, client):
    d = world["a"]["d"]
    _, cid, _ = chat(d, "visitor-pilot-1", "Is the Civic available?")
    run_worker()
    conv = db.get(Conversation, cid)
    m1 = queue_outbound(db, conv, "hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, m1.id).delivery_status == "SIMULATED"  # not authorized → visitor never sees it
    assert client.get(f"/api/chat/{d.public_chat_key}/messages", params={"visitor_id": "visitor-pilot-1"}).json()["messages"] == []
    controls.authorize_live(db, "customer_messaging.WEBSITE_CHAT", user_id=world["platform"].id,
                            statement="pilot: internal testers only on chat", dealership_id=d.id)
    db.commit()
    m2 = queue_outbound(db, conv, "hi again", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, m2.id).delivery_status == "BLOCKED"  # live, but visitor not allowlisted
    db.add(PilotRecipient(kind="CHAT_VISITOR", value="visitor-pilot-1", label="QA browser"))
    db.commit()
    m3 = queue_outbound(db, conv, "allowlisted hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    msgs = client.get(f"/api/chat/{d.public_chat_key}/messages", params={"visitor_id": "visitor-pilot-1"}).json()["messages"]
    assert [x["text"] for x in msgs] == ["allowlisted hello"] and m3.id

"""Stage 4: Messenger and Twilio send adapters against mocked HTTP only, and the rules for when they are used."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from dealerflow.config import get_settings
from dealerflow.integrations import customer_channels as ch
from tests.customer_helpers import world  # noqa: F401


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_messenger_adapter_outcomes():
    seen = []

    def ok(req):
        seen.append(req)
        return httpx.Response(200, json={"message_id": "m_1"})

    r = ch.MessengerChannel("123", "tok", "v24.0", client=_client(ok)).send_text("psid", "hi")
    assert r.status == "SENT" and r.provider_message_id == "m_1"
    assert seen[0].url.params["access_token"] == "tok" and b'"RESPONSE"' in seen[0].content
    r = ch.MessengerChannel("123", "tok", "v24.0", client=_client(ok)).send_text("psid", "hi", human_agent=True)
    assert b"HUMAN_AGENT" in seen[1].content
    limited = ch.MessengerChannel("1", "t", "v24.0", client=_client(lambda q: httpx.Response(400, json={"error": {"code": 613, "message": "rate"}})))
    assert limited.send_text("p", "x").retryable
    bad = ch.MessengerChannel("1", "t", "v24.0", client=_client(lambda q: httpx.Response(400, json={"error": {"code": 100, "message": "bad"}})))
    res = bad.send_text("p", "x")
    assert not res.ok and not res.retryable and not res.uncertain

    def timeout(req):
        raise httpx.ReadTimeout("read timed out", request=req)

    res = ch.MessengerChannel("1", "t", "v24.0", client=_client(timeout)).send_text("p", "x")
    assert res.uncertain and not res.retryable


def test_twilio_adapter_outcomes():
    seen = []

    def ok(req):
        seen.append(req)
        return httpx.Response(201, json={"sid": "SM1"})

    r = ch.TwilioSmsChannel("AC1", "tok", "+12815550100", client=_client(ok)).send_text("+12815550199", "hi")
    assert r.status == "SENT" and r.provider_message_id == "SM1" and b"To=%2B12815550199" in seen[0].content
    stop = ch.TwilioSmsChannel("AC1", "t", "+1", client=_client(lambda q: httpx.Response(400, json={"code": 21610, "message": "unsubscribed"})))
    res = stop.send_text("+1", "x")
    assert not res.ok and "21610" in res.error and not res.retryable


def test_live_adapters_need_switch_authorized_connection_and_env_credentials(world, db, monkeypatch):
    from dealerflow.models_ops import IntegrationConnection

    a = world["a"]["id"]
    conn = IntegrationConnection(client_id=a, kind="customer_messaging", provider="meta_messenger", status="connected",
                                 external_account_id="9001", config={"token_env": "DF_TEST_PAGE_TOKEN"})
    db.add(conn)
    db.commit()
    assert not ch.resolve(db, a, ch.META_MESSENGER).live  # master switch off
    monkeypatch.setenv("DF_LIVE_CUSTOMER_MESSAGING_ENABLED", "true")
    get_settings.cache_clear()
    r = ch.resolve(db, a, ch.META_MESSENGER)
    assert not r.live and "credentials" in r.note  # env var not set
    monkeypatch.setenv("DF_TEST_PAGE_TOKEN", "secret-token")
    r = ch.resolve(db, a, ch.META_MESSENGER)
    assert r.live and isinstance(r.adapter, ch.MessengerChannel)
    conn.config = {"token_env": "PATH"}  # only DF_* variables may be referenced
    db.commit()
    assert not ch.resolve(db, a, ch.META_MESSENGER).live
    conn.status = "sandbox"
    db.commit()
    assert not ch.resolve(db, a, ch.META_MESSENGER).live
    assert not ch.resolve(db, world["b"]["id"], ch.META_MESSENGER).live  # another dealership never uses this connection


@pytest.mark.parametrize("channel", ["META_MESSENGER", "TWILIO_SMS"])
def test_live_send_still_goes_through_pilot_gate(world, db, monkeypatch, channel):
    from dealerflow.models_customer import CustomerConversation, CustomerMessage
    from dealerflow.models_dealer import Client
    from dealerflow.models_ops import IntegrationConnection
    from dealerflow.services.customer.outbound import queue_outbound
    from dealerflow.services.customer.processing import ingest_inbound
    from tests.customer_helpers import run_worker

    calls = []
    monkeypatch.setattr(ch.MessengerChannel, "send_text", lambda self, *a, **k: calls.append(a) or ch.SendResult("SENT", "m"))
    monkeypatch.setattr(ch.TwilioSmsChannel, "send_text", lambda self, *a, **k: calls.append(a) or ch.SendResult("SENT", "s"))
    monkeypatch.setenv("DF_LIVE_CUSTOMER_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("DF_T_TOKEN", "t")
    monkeypatch.setenv("DF_T_SID", "AC1")
    get_settings.cache_clear()
    a = world["a"]["id"]
    db.add(IntegrationConnection(client_id=a, kind="customer_messaging", provider=ch.PROVIDER_NAME[channel], status="connected",
                                 external_account_id="+12815550100" if channel == "TWILIO_SMS" else "9001",
                                 config={"token_env": "DF_T_TOKEN", "account_sid_env": "DF_T_SID"}))
    db.commit()
    res = ingest_inbound(db, dealership=db.get(Client, a), channel=channel, external_conversation_id="+12815550177", text="Hi",
                         provider_message_id=f"pm-{channel}")
    conv = db.get(CustomerConversation, res.conversation.id)
    queue_outbound(db, conv, "Thanks for reaching out to Alpha Auto (TEST).", sender_type="STAFF", user_id=world["a"]["reviewer"])
    db.commit()
    run_worker()
    db.expire_all()
    out = db.scalar(select(CustomerMessage).where(CustomerMessage.direction == "OUTBOUND"))
    assert out.delivery_status == "BLOCKED" and "pilot mode" in out.delivery_error and calls == []

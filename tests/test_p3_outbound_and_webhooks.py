"""Phase 3 webhook signatures (Meta, Twilio), tenant routing, duplicate webhooks and failed sandbox sends.
Live Messenger/Twilio send adapters are not part of this build, so there are no adapter tests."""

import hashlib
import hmac
import json

import pytest
from sqlalchemy import func, select

from dealerflow.config import get_settings
from dealerflow.models import Alert, IntegrationConnection, Job
from dealerflow.models_conversations import Conversation
from dealerflow.models_conversations import ConversationMessage as Message
from dealerflow.services.conversations.channels import twilio_signature, verify_twilio_signature
from dealerflow.services.conversations.outbound import queue_outbound
from tests.phase3 import build_world, chat, run_worker


@pytest.fixture
def world(db):
    return build_world(db)


def test_twilio_signature_helpers():
    params = {"From": "+18325550142", "To": "+12815550100", "Body": "hi", "MessageSid": "SM9"}
    sig = twilio_signature("secret", "https://x.test/webhooks/twilio/sms", params)
    assert verify_twilio_signature("secret", "https://x.test/webhooks/twilio/sms", params, sig)
    assert not verify_twilio_signature("secret", "https://x.test/webhooks/twilio/sms", {**params, "Body": "x"}, sig)
    assert not verify_twilio_signature("", "https://x.test/webhooks/twilio/sms", params, sig)


def test_meta_webhook_signature_tenant_routing_and_dedupe(db, world, client, monkeypatch):
    monkeypatch.setenv("DF_META_APP_SECRET", "appsecret")
    monkeypatch.setenv("DF_META_VERIFY_TOKEN", "verify")
    get_settings.cache_clear()
    db.add(IntegrationConnection(client_id=world["b"]["d"].id, kind="customer_messaging", provider="meta_messenger",
                                 external_account_id="PAGE_B", status="sandbox", config={}))
    db.commit()
    r = client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify", "hub.challenge": "42"})
    assert r.text == "42"
    assert client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "nope"}).status_code == 403
    payload = {"object": "page", "entry": [
        {"id": "PAGE_B", "messaging": [{"sender": {"id": "PSID1"}, "timestamp": 1760000000000,
                                        "message": {"mid": "mid.1", "text": "Is the Civic available?"}}]},
        {"id": "UNKNOWN_PAGE", "messaging": [{"sender": {"id": "PSID2"}, "timestamp": 1760000000000,
                                              "message": {"mid": "mid.2", "text": "hello"}}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"appsecret", raw, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/meta", content=raw, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403
    for _ in range(2):  # Meta may deliver the same event twice
        r = client.post("/webhooks/meta", content=raw, headers={"x-hub-signature-256": sig, "content-type": "application/json"})
        assert r.status_code == 200
    msgs = list(db.scalars(select(Message)))
    assert len(msgs) == 1 and msgs[0].client_id == world["b"]["d"].id  # unknown page ignored, dupe dropped
    get_settings.cache_clear()


def test_meta_webhook_refused_without_configured_secret(client):
    get_settings.cache_clear()
    assert client.post("/webhooks/meta", content=b"{}", headers={"x-hub-signature-256": "sha256=00"}).status_code == 403


def test_twilio_webhook_requires_valid_signature(db, world, client, monkeypatch):
    monkeypatch.setenv("DF_TWILIO_TOKEN_ALPHA", "tok")
    db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="twilio_sms",
                                 external_account_id="+12815550100", status="sandbox",
                                 config={"auth_token_env": "DF_TWILIO_TOKEN_ALPHA"}))
    db.commit()
    params = {"From": "+18325550142", "To": "+12815550100", "Body": "ALTO", "MessageSid": "SM1"}
    url = f"{get_settings().base_url.rstrip('/')}/webhooks/twilio/sms"  # what Twilio signs: the public URL
    assert client.post("/webhooks/twilio/sms", data=params, headers={"x-twilio-signature": "bad"}).status_code == 403
    sig = twilio_signature("tok", url, params)
    assert client.post("/webhooks/twilio/sms", data=params, headers={"x-twilio-signature": sig}).status_code == 200
    run_worker()
    conv = db.scalar(select(Conversation).where(Conversation.channel == "TWILIO_SMS"))
    assert conv.opted_out  # Spanish keyword recognized independently of Twilio's English defaults


def test_twilio_token_env_must_be_namespaced(db, world, client, monkeypatch):
    """A connection cannot point the webhook at an arbitrary env var (e.g. the app's secret key)."""
    monkeypatch.setenv("DF_SECRET_KEY", "known-value")
    db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="twilio_sms",
                                 external_account_id="+12815550101", status="sandbox", config={"auth_token_env": "DF_SECRET_KEY"}))
    db.commit()
    params = {"From": "+18325550142", "To": "+12815550101", "Body": "hi", "MessageSid": "SM2"}
    sig = twilio_signature("known-value", f"{get_settings().base_url.rstrip('/')}/webhooks/twilio/sms", params)
    assert client.post("/webhooks/twilio/sms", data=params, headers={"x-twilio-signature": sig}).status_code == 403


def _queue(db, world, visitor, **config):
    _, cid, _ = chat(world["a"]["d"], visitor, "Is the Civic available?")
    run_worker()
    db.expire_all()
    conv = db.get(Conversation, cid)
    conv.channel = "TWILIO_SMS"  # a provider-backed channel (sandbox adapter)
    conv.external_conversation_id = "+18325550000"
    conn = IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="twilio_sms",
                                 external_account_id="+12815550100", status="sandbox", config=config)
    db.add(conn)
    db.commit()
    return conv, conn


def test_retryable_failure_retries_then_marks_failed_and_alerts(db, world):
    conv, _ = _queue(db, world, "f-1", sandbox_failure_mode="retryable")
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker(rounds=12)
    db.expire_all()
    m = db.get(Message, msg.id)
    job = db.scalar(select(Job).where(Job.kind == "conversations.outbound_send"))
    assert job.status == "failed" and job.attempts == job.max_attempts
    assert m.delivery_status == "FAILED" and "503" in (m.delivery_error or "")
    assert db.scalar(select(func.count(Alert.id))) >= 1


def test_permanent_failure_fails_fast(db, world):
    conv, _ = _queue(db, world, "f-2", sandbox_failure_mode="permanent")
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "FAILED"
    assert db.scalar(select(Job).where(Job.kind == "conversations.outbound_send")).attempts == 1


def test_uncertain_send_is_never_resent_automatically(db, world):
    conv, _ = _queue(db, world, "f-4", sandbox_failure_mode="uncertain")
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "UNCERTAIN"
    assert db.scalar(select(Job).where(Job.kind == "conversations.outbound_send")).attempts == 1


def test_sandbox_send_marked_simulated(db, world):
    conv, _ = _queue(db, world, "f-3")
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    m = db.get(Message, msg.id)
    assert m.delivery_status == "SIMULATED" and "simulated" in (m.delivery_error or "").lower()

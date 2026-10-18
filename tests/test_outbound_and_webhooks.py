"""Provider adapters (mocked HTTP), webhook signatures, duplicate webhooks, failed messaging APIs."""

import hashlib
import hmac
import json

import httpx
from sqlalchemy import func, select

from app.config import get_settings
from app.integrations import meta_messenger, twilio_sms
from app.models import Conversation, IntegrationConnection, Job, Message, Notification
from app.security.crypto import encrypt_json
from app.services.inbox.outbound import queue_outbound
from tests.conftest import chat, run_worker


def _mock(status, body):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body)))


def test_messenger_adapter_results():
    ok = meta_messenger.MessengerChannel("p1", "tok", client=_mock(200, {"message_id": "m.1"})).send_text("u", "hi")
    assert ok.status == "SENT" and ok.provider_message_id == "m.1"
    err5 = meta_messenger.MessengerChannel("p1", "tok", client=_mock(500, {"error": {"message": "x"}})).send_text("u", "hi")
    assert err5.status == "FAILED" and err5.retryable
    err4 = meta_messenger.MessengerChannel("p1", "tok", client=_mock(400, {"error": {"code": 10, "message": "window"}})).send_text("u", "hi")
    assert not err4.retryable
    rl = meta_messenger.MessengerChannel("p1", "tok", client=_mock(400, {"error": {"code": 613, "message": "rate"}})).send_text("u", "hi")
    assert rl.retryable


def test_twilio_adapter_and_signature():
    ok = twilio_sms.TwilioSmsChannel("AC1", "t", "+1281", client=_mock(201, {"sid": "SM1"})).send_text("+1832", "hi")
    assert ok.status == "SENT"
    params = {"From": "+18325550142", "To": "+12815550100", "Body": "hi", "MessageSid": "SM9"}
    sig = twilio_sms.compute_signature("secret", "https://x.test/webhooks/twilio/sms", params)
    assert twilio_sms.validate_signature("secret", "https://x.test/webhooks/twilio/sms", params, sig)
    assert not twilio_sms.validate_signature("secret", "https://x.test/webhooks/twilio/sms", {**params, "Body": "x"}, sig)


def test_meta_webhook_signature_tenant_routing_and_dedupe(db, world, client, monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "appsecret")
    monkeypatch.setenv("META_VERIFY_TOKEN", "verify")
    get_settings.cache_clear()
    db.add(IntegrationConnection(dealership_id=world["b"]["d"].id, provider="META_MESSENGER", external_account_id="PAGE_B",
                                 status="SANDBOX"))
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
    assert len(msgs) == 1 and msgs[0].dealership_id == world["b"]["d"].id  # unknown page ignored, dupe dropped
    get_settings.cache_clear()


def test_twilio_webhook_requires_valid_signature(db, world, client):
    db.add(IntegrationConnection(dealership_id=world["a"]["d"].id, provider="TWILIO_SMS", external_account_id="+12815550100",
                                 status="SANDBOX", encrypted_credentials=encrypt_json({"account_sid": "AC1", "auth_token": "tok"})))
    db.commit()
    params = {"From": "+18325550142", "To": "+12815550100", "Body": "ALTO", "MessageSid": "SM1"}
    url = "http://testserver/webhooks/twilio/sms"
    assert client.post("/webhooks/twilio/sms", data=params, headers={"x-twilio-signature": "bad"}).status_code == 403
    sig = twilio_sms.compute_signature("tok", url, params)
    assert client.post("/webhooks/twilio/sms", data=params, headers={"x-twilio-signature": sig}).status_code == 200
    run_worker()
    conv = db.scalar(select(Conversation).where(Conversation.channel == "TWILIO_SMS"))
    assert conv.opted_out  # Spanish keyword recognized independently of Twilio's English defaults


def _queue(db, world, visitor):
    _, cid, _ = chat(world["a"]["d"], visitor, "Is the Civic available?")
    run_worker()
    conv = db.get(Conversation, cid)
    conv.channel = "TWILIO_SMS"  # use a provider-backed channel (sandbox adapter)
    conv.external_conversation_id = "+18325550000"
    conn = IntegrationConnection(dealership_id=world["a"]["d"].id, provider="TWILIO_SMS", external_account_id="+12815550100",
                                 status="SANDBOX")
    db.add(conn)
    db.commit()
    return conv, conn


def test_retryable_failure_retries_then_marks_failed_and_notifies(db, world):
    conv, conn = _queue(db, world, "f-1")
    conn.config = {"sandbox_failure_mode": "retryable"}
    db.commit()
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    from sqlalchemy import update

    from app.db import utcnow
    for _ in range(6):
        db.execute(update(Job).where(Job.status == "QUEUED").values(run_at=utcnow()))
        db.commit()
        run_worker()
    db.expire_all()
    m = db.get(Message, msg.id)
    job = db.scalar(select(Job).where(Job.type == "outbound.send", Job.payload["message_id"].as_integer() == msg.id))
    assert job.status == "DEAD" and job.attempts == job.max_attempts
    assert m.delivery_status == "FAILED" and "503" in (m.delivery_error or "")
    assert db.scalar(select(func.count(Notification.id)).where(Notification.subject.contains("failed to send"))) >= 1


def test_permanent_failure_fails_fast(db, world):
    conv, conn = _queue(db, world, "f-2")
    conn.config = {"sandbox_failure_mode": "permanent"}
    db.commit()
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "FAILED"


def test_sandbox_send_marked_simulated(db, world):
    conv, _ = _queue(db, world, "f-3")
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    m = db.get(Message, msg.id)
    assert m.delivery_status == "SIMULATED" and "simulated" in m.delivery_error

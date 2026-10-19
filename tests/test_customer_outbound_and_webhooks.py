"""Webhook signatures, tenant routing, duplicate webhooks, failed messaging APIs.

Ported from app/tests/test_outbound_and_webhooks.py. dealerflow has no live Meta Messenger / Twilio adapters in this
build (both resolve to the sandbox channel), so the old adapter result tests (mocked HTTP) are left out; the
Twilio signature check is exercised through ``routes_customer_public.twilio_signature`` and the webhook itself.
Webhook secrets are the platform settings DF_META_APP_SECRET / DF_META_VERIFY_TOKEN / DF_TWILIO_AUTH_TOKEN.
Synthetic data only.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from sqlalchemy import func, select, update

from dealerflow.config import get_settings
from dealerflow.db import utcnow
from dealerflow.models import Job
from dealerflow.models_customer import (
    CustomerConversation,
    CustomerMessage,
    DealerNotification,
)
from dealerflow.models_ops import IntegrationConnection
from dealerflow.services.customer.outbound import queue_outbound
from dealerflow.web.routes_customer_public import twilio_signature
from tests.customer_helpers import chat, run_worker, world  # noqa: F401


def test_twilio_signature():
    """Old test_twilio_adapter_and_signature: the adapter half has no dealerflow equivalent."""
    params = {"From": "+18325550142", "To": "+12815550100", "Body": "hi", "MessageSid": "SM9"}
    sig = twilio_signature("secret", "https://x.test/webhooks/twilio/sms", params)
    assert hmac.compare_digest(sig, twilio_signature("secret", "https://x.test/webhooks/twilio/sms", dict(params)))
    assert twilio_signature("secret", "https://x.test/webhooks/twilio/sms", {**params, "Body": "x"}) != sig
    assert twilio_signature("other", "https://x.test/webhooks/twilio/sms", params) != sig


def test_meta_webhook_signature_tenant_routing_and_dedupe(db, world, client, monkeypatch):
    monkeypatch.setenv("DF_META_APP_SECRET", "appsecret")
    monkeypatch.setenv("DF_META_VERIFY_TOKEN", "verify")
    get_settings.cache_clear()
    db.add(IntegrationConnection(client_id=world["b"]["id"], kind="customer_messaging", provider="meta_messenger",
                                 external_account_id="PAGE_B", status="sandbox"))
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
    db.expire_all()
    msgs = list(db.scalars(select(CustomerMessage)))
    assert len(msgs) == 1 and msgs[0].client_id == world["b"]["id"]  # unknown page ignored, dupe dropped
    get_settings.cache_clear()


def test_twilio_webhook_requires_valid_signature(db, world, client, monkeypatch):
    monkeypatch.setenv("DF_TWILIO_AUTH_TOKEN", "tok")
    get_settings.cache_clear()
    db.add(IntegrationConnection(client_id=world["a"]["id"], kind="customer_messaging", provider="twilio_sms",
                                 external_account_id="+12815550100", status="sandbox"))
    db.commit()
    params = {"From": "+18325550142", "To": "+12815550100", "Body": "ALTO", "MessageSid": "SM1"}
    url = f"{get_settings().base_url.rstrip('/')}/webhooks/twilio/sms"
    assert client.post("/webhooks/twilio/sms", data=params, headers={"x-twilio-signature": "bad"}).status_code == 403
    sig = twilio_signature("tok", url, params)
    assert client.post("/webhooks/twilio/sms", data=params, headers={"x-twilio-signature": sig}).status_code == 200
    run_worker()
    db.expire_all()
    conv = db.scalar(select(CustomerConversation).where(CustomerConversation.channel == "TWILIO_SMS"))
    assert conv.opted_out  # Spanish keyword recognized independently of Twilio's English defaults
    get_settings.cache_clear()


def _queue(db, world, visitor):
    _, cid, _ = chat(world["a"]["id"], visitor, "Is the Civic available?")
    run_worker()
    db.expire_all()
    conv = db.get(CustomerConversation, cid)
    conv.channel = "TWILIO_SMS"  # use a provider-backed channel (sandbox adapter)
    conv.external_conversation_id = "+18325550000"
    conn = IntegrationConnection(client_id=world["a"]["id"], kind="customer_messaging", provider="twilio_sms",
                                 external_account_id="+12815550100", status="sandbox")
    db.add(conn)
    db.commit()
    return conv, conn


def test_retryable_failure_retries_then_marks_failed_and_notifies(db, world):
    conv, conn = _queue(db, world, "f-1")
    conn.config = {"sandbox_failure_mode": "retryable"}
    db.commit()
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["reviewer"])
    db.commit()
    for _ in range(6):
        db.execute(update(Job).where(Job.status == "pending").values(run_after=utcnow()))
        db.commit()
        run_worker()
    db.expire_all()
    m = db.get(CustomerMessage, msg.id)
    job = db.scalar(select(Job).where(Job.kind == "customer.send", Job.key == f"customer.send:{msg.id}"))
    assert job.status == "failed" and job.attempts == job.max_attempts
    assert m.delivery_status == "FAILED" and "503" in (m.delivery_error or "")
    assert db.scalar(select(func.count(DealerNotification.id)).where(DealerNotification.subject.contains("failed to send"))) >= 1


def test_permanent_failure_fails_fast(db, world):
    conv, conn = _queue(db, world, "f-2")
    conn.config = {"sandbox_failure_mode": "permanent"}
    db.commit()
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["reviewer"])
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(CustomerMessage, msg.id).delivery_status == "FAILED"


def test_sandbox_send_marked_simulated(db, world):
    conv, _ = _queue(db, world, "f-3")
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["reviewer"])
    db.commit()
    run_worker()
    db.expire_all()
    m = db.get(CustomerMessage, msg.id)
    assert m.delivery_status == "SIMULATED" and "simulated" in m.delivery_error

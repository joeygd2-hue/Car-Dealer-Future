"""Phase 3 inside the unified application: client deletion and export, retention purge, Meta data-deletion
callback, emergency stop on notification email, raw webhook privacy, tenant composite keys."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from dealerflow.config import get_settings
from dealerflow.db import session_factory, utcnow
from dealerflow.models import AgentState, Client, IntegrationConnection, User, WebhookEvent
from dealerflow.models_conversations import (
    Appointment,
    Conversation,
    ConversationMessage,
    CustomerContact,
    Notification,
    ResponseDraft,
)
from dealerflow.services import maintenance
from tests.phase3 import build_world, chat, run_due, run_worker


@pytest.fixture
def world(db):
    return build_world(db)


def _booking(world, visitor="int-1"):
    _, cid, _ = chat(world["a"]["d"], visitor, "I'd like to test drive the Civic Saturday at 11am. I'm Ana, 281-555-0101")
    run_due()
    return cid


def test_delete_client_removes_phase2_and_phase3_rows(db, world, tmp_path):
    cid = _booking(world)
    a, b = world["a"]["d"].id, world["b"]["d"].id
    _booking({"a": world["b"]}, "int-b")
    admin = db.scalar(select(User).where(User.role == "admin"))
    export = maintenance.export_client(db, a)
    assert export["conversations"] and export["conversation_messages"] and export["appointments"]
    maintenance.delete_client(db, a, "Alpha Auto", admin, str(tmp_path))
    db.commit()
    for model in (Conversation, ConversationMessage, Appointment, CustomerContact, ResponseDraft):
        assert db.scalar(select(func.count()).select_from(model).where(model.client_id == a)) == 0, model
    assert db.get(Conversation, cid) is None
    assert db.scalar(select(func.count(Conversation.id)).where(Conversation.client_id == b)) == 1  # Bravo untouched


def test_retention_purge_removes_customer_text_but_keeps_counts(db, world):
    cid = _booking(world, "ret-1")
    db.execute(update(Conversation).where(Conversation.id == cid).values(created_at=utcnow() - timedelta(days=400)))
    db.commit()
    assert maintenance.purge_conversation_pii(db, 365) == 1
    db.commit()
    db.expire_all()
    assert {m.body for m in db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == cid))} == {"[purged]"}
    appt = db.scalar(select(Appointment).where(Appointment.conversation_id == cid))
    assert appt.customer_name is None and appt.customer_phone is None
    conv = db.get(Conversation, cid)
    contact = db.get(CustomerContact, conv.contact_id)
    assert contact.display_name is None and contact.phone is None and conv.pii_purged_at
    assert maintenance.purge_conversation_pii(db, 365) == 0  # idempotent


def test_meta_data_deletion_callback(db, world, client, monkeypatch):
    monkeypatch.setenv("DF_META_APP_SECRET", "appsecret")
    get_settings.cache_clear()
    db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="meta_messenger",
                                 external_account_id="PAGE_A", status="sandbox", config={}))
    db.commit()
    from dealerflow.services.conversations.inbox import ingest_inbound

    ingest_inbound(db, client=db.get(Client, world["a"]["d"].id), channel="META_MESSENGER", external_conversation_id="PSID-9",
                   text="Is the Civic available? I'm Ana", provider_message_id="mid.9")
    db.commit()

    def signed(payload: dict, secret: bytes = b"appsecret") -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(hmac.new(secret, body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        return f"{sig}.{body}"

    assert client.post("/webhooks/meta/data-deletion", data={"signed_request": signed({"user_id": "PSID-9"}, b"wrong")}).status_code == 400
    r = client.post("/webhooks/meta/data-deletion", data={"signed_request": signed({"user_id": "PSID-9"})})
    assert r.status_code == 200 and r.json()["confirmation_code"]
    db.expire_all()
    assert db.scalar(select(func.count(Conversation.id)).where(Conversation.channel == "META_MESSENGER")) == 0
    get_settings.cache_clear()


def test_raw_webhook_events_do_not_store_message_text(db, world, client, monkeypatch):
    monkeypatch.setenv("DF_META_APP_SECRET", "appsecret")
    get_settings.cache_clear()
    db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="meta_messenger",
                                 external_account_id="PAGE_A", status="sandbox", config={}))
    db.commit()
    payload = {"object": "page", "entry": [{"id": "PAGE_A", "messaging": [
        {"sender": {"id": "PSID1"}, "timestamp": 1760000000000, "message": {"mid": "mid.1", "text": "my phone is 832-555-0199"}}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"appsecret", raw, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/meta", content=raw, headers={"x-hub-signature-256": sig}).status_code == 200
    events = list(db.scalars(select(WebhookEvent).where(WebhookEvent.provider == "META")))
    assert len(events) == 1 and "832-555-0199" not in json.dumps(events[0].payload)
    get_settings.cache_clear()


def test_emergency_stop_blocks_notification_email(db, world):
    from dealerflow.integrations.email import MockEmailProvider
    from dealerflow.services.conversations import notifications

    state = db.get(AgentState, 1) or AgentState(id=1)
    state.status = "emergency_stopped"
    db.add(state)
    db.commit()
    MockEmailProvider.outbox.clear()
    notifications.notify(db, world["a"]["d"].id, kind="ESCALATION", subject="stop-test", body="b", escalation_reasons=["complaint"])
    db.commit()
    run_worker()
    db.expire_all()
    emails = list(db.scalars(select(Notification).where(Notification.subject == "stop-test", Notification.channel == "EMAIL")))
    assert emails and all(n.status == "BLOCKED" and "emergency stop" in n.status_detail for n in emails)
    assert MockEmailProvider.outbox == []


def test_conversation_rows_cannot_reference_another_dealerships_rows(db, world):
    cid = _booking(world, "fk-1")
    s = session_factory()()
    s.add(ConversationMessage(client_id=world["b"]["d"].id, conversation_id=cid, channel="WEBSITE_CHAT", direction="NOTE",
                              sender_type="SYSTEM", body="x"))
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()
    s.close()


def test_simulated_dealership_conversations_are_simulated(db, world):
    c = db.get(Client, world["a"]["d"].id)
    c.simulated = True
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "sim-1", "Is the Civic available?")
    assert db.get(Conversation, cid).simulated is True

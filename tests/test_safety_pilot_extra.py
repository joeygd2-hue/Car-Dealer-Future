"""Stage 4 pilot-mode guarantees not covered by test_safety_pilot.py (ported from app/tests/test_pilot_mode.py and the
notification-reconciliation case of app/tests/test_role_matrix.py).

Old ``controls.set_flag(EMERGENCY_STOP_ALL)`` is dealerflow's platform emergency stop (``agent.set_status``); old
``LiveAuthorization`` for customer messaging is a ``customer_messaging`` connection with status ``connected``; old
``Notification`` is ``DealerNotification``. No provider is contacted: website chat is first-party and the Messenger
adapter is replaced by a recorder. Synthetic data only.
"""

from __future__ import annotations

from sqlalchemy import select

from dealerflow.config import get_settings
from dealerflow.integrations import customer_channels as ch
from dealerflow.integrations.email import MockEmailProvider
from dealerflow.integrations.errors import IntegrationError
from dealerflow.models import AuditLog, Job, PilotRecipient, User
from dealerflow.models_customer import (
    CustomerConversation,
    CustomerMessage,
    DealerNotification,
    ResponseAutomationSettings,
    SendAttempt,
)
from dealerflow.models_dealer import Client
from dealerflow.models_ops import IntegrationConnection
from dealerflow.services import agent, pilot
from dealerflow.services.customer import notify as notifications
from dealerflow.services.customer.outbound import queue_outbound
from dealerflow.services.customer.processing import ingest_inbound
from tests.conftest import csrf_of, login
from tests.customer_helpers import chat, run_worker, world  # noqa: F401


def _attempt(db, msg_id):
    return db.scalar(select(SendAttempt).where(SendAttempt.kind == "CUSTOMER_MESSAGE", SendAttempt.ref_id == msg_id)
                     .order_by(SendAttempt.id.desc()))


def _visible_to_visitor(client, db, client_id, visitor):
    key = db.scalar(select(ResponseAutomationSettings.chat_widget_key).where(ResponseAutomationSettings.client_id == client_id))
    r = client.get(f"/api/chat/{key}/messages", params={"visitor_id": visitor})
    assert r.status_code == 200
    return [m["text"] for m in r.json()["messages"]]


def _live_website_chat(db, monkeypatch, client_id):
    monkeypatch.setenv("DF_LIVE_CUSTOMER_MESSAGING_ENABLED", "true")
    get_settings.cache_clear()
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == client_id,
                                                         IntegrationConnection.provider == "website_chat"))
    conn.status = "connected"
    db.commit()


def _staff_reply(db, conv_id, text, user_id):
    msg = queue_outbound(db, db.get(CustomerConversation, conv_id), text, sender_type="STAFF", user_id=user_id)
    db.commit()
    run_worker()
    db.expire_all()
    return db.get(CustomerMessage, msg.id)


# ------------------------------------------------------------------ emergency stop beats the allowlist


def test_emergency_stop_blocks_an_allowlisted_live_send(client, db, world, monkeypatch):
    a = world["a"]
    _live_website_chat(db, monkeypatch, a["id"])
    pilot.add(db, "CHAT_VISITOR", "tester-stop-01", label="internal tester", actor_id=world["staff_admin"])
    db.commit()
    _, conv_id, _ = chat(a["id"], "tester-stop-01", "Is the Civic still available?")
    admin = db.get(User, world["staff_admin"])
    agent.set_status(db, "emergency_stopped", actor=admin, reason="drill")
    db.commit()

    blocked = _staff_reply(db, conv_id, "Yes, it is available.", a["reviewer"])
    assert blocked.delivery_status == "BLOCKED" and "emergency stop" in blocked.delivery_error
    att = _attempt(db, blocked.id)
    assert att.live and att.status == "BLOCKED" and any("emergency stop" in r for r in att.reasons)
    assert _visible_to_visitor(client, db, a["id"], "tester-stop-01") == []  # the visitor never sees it

    # Same recipient and setup once the stop is lifted: the reply is delivered (so the block above was the stop).
    agent.set_status(db, "running", actor=admin, reason="drill complete")
    db.commit()
    sent = _staff_reply(db, conv_id, "Thanks for waiting, it is available.", a["reviewer"])
    assert sent.delivery_status == "SENT" and _attempt(db, sent.id).live
    assert _visible_to_visitor(client, db, a["id"], "tester-stop-01") == ["Thanks for waiting, it is available."]


# ------------------------------------------------------------------ no authorization = simulated, whatever the env says


def test_no_live_authorization_means_simulated_even_with_env_switch_on(client, db, world, monkeypatch):
    """Website chat: DF_LIVE_CUSTOMER_MESSAGING_ENABLED=true and an allowlisted visitor, but the dealership's
    connection is not authorized (status sandbox) → SIMULATED, never shown to the visitor."""
    a = world["a"]
    monkeypatch.setenv("DF_LIVE_CUSTOMER_MESSAGING_ENABLED", "true")
    get_settings.cache_clear()
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == a["id"],
                                                         IntegrationConnection.provider == "website_chat"))
    assert conn.status == "sandbox"
    pilot.add(db, "CHAT_VISITOR", "tester-noauth-1", label="internal tester", actor_id=world["staff_admin"])
    db.commit()
    _, conv_id, _ = chat(a["id"], "tester-noauth-1", "Are you open Saturday?")
    m = _staff_reply(db, conv_id, "Yes, 9 to 7.", a["reviewer"])
    assert m.delivery_status == "SIMULATED"
    att = _attempt(db, m.id)
    assert att.status == "SIMULATED" and not att.live
    assert _visible_to_visitor(client, db, a["id"], "tester-noauth-1") == []


def test_no_live_authorization_means_simulated_for_messenger_too(db, world, monkeypatch):
    calls = []
    monkeypatch.setattr(ch.MessengerChannel, "send_text", lambda self, *a, **k: calls.append(a) or ch.SendResult("SENT", "m.1"))
    monkeypatch.setenv("DF_LIVE_CUSTOMER_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("DF_T_PAGE_TOKEN", "tok")
    get_settings.cache_clear()
    a = world["a"]["id"]
    conn = IntegrationConnection(client_id=a, kind="customer_messaging", provider="meta_messenger", status="sandbox",
                                 external_account_id="PAGE_A", config={"token_env": "DF_T_PAGE_TOKEN"})
    db.add(conn)
    pilot.add(db, "META_PSID", "PSID_OPS_TEST", label="Ops test account", actor_id=world["staff_admin"])
    db.commit()
    res = ingest_inbound(db, dealership=db.get(Client, a), channel="META_MESSENGER", external_conversation_id="PSID_OPS_TEST",
                         text="Is the Civic available?", provider_message_id="mid.noauth.1")
    db.commit()
    m = _staff_reply(db, res.conversation.id, "Yes it is.", world["a"]["reviewer"])
    assert m.delivery_status == "SIMULATED" and calls == []
    assert not _attempt(db, m.id).live
    # Control: authorizing the connection makes the very same send live (it reaches the recorder).
    conn.status = "connected"
    db.commit()
    m2 = _staff_reply(db, res.conversation.id, "Still available.", world["a"]["reviewer"])
    assert m2.delivery_status == "SENT" and len(calls) == 1 and _attempt(db, m2.id).live


# ------------------------------------------------------------------ console: allowlist admin-only, reconcile notifications


def test_allowlist_management_is_agency_admin_only(client, db, world):
    login(client, "manager@agency.example")
    r = client.post("/admin/pilot/allowlist", data={"kind": "EMAIL", "value": "x@y.example", "label": "nope", "csrf": csrf_of(client)})
    assert r.status_code == 403
    db.expire_all()
    assert db.scalar(select(PilotRecipient)) is None
    client.cookies.clear()
    login(client, "admin@agency.example")
    r = client.post("/admin/pilot/allowlist", data={"kind": "EMAIL", "value": "QA@Internal.example", "label": "QA inbox",
                                                    "csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    row = db.scalar(select(PilotRecipient))
    assert (row.kind, row.value) == ("EMAIL", "qa@internal.example")


def _uncertain_notification(db, world) -> DealerNotification:
    MockEmailProvider.fail_next = [IntegrationError("read timeout after the request was sent")]
    notifications.notify(db, world["a"]["id"], kind="ESCALATION", subject="unc", body="b", dedupe_key="unc")
    db.commit()
    run_worker()
    db.expire_all()
    n = db.scalar(select(DealerNotification).where(DealerNotification.subject == "unc", DealerNotification.channel == "EMAIL",
                                                   DealerNotification.recipient_address == "admin@a.example"))
    assert n.status == "UNCERTAIN"
    return n


def test_uncertain_notification_reconciled_as_delivered(client, db, world):
    n = _uncertain_notification(db, world)
    login(client, "admin@agency.example")
    assert f"Notification #{n.id}" in client.get("/admin/pilot").text
    r = client.post(f"/admin/pilot/reconcile/notification/{n.id}", data={"delivered": 1, "csrf": csrf_of(client)},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(DealerNotification, n.id).status == "SENT"
    # The ledger keeps what actually happened at send time.
    att = db.scalar(select(SendAttempt).where(SendAttempt.kind == "DEALER_NOTIFICATION_EMAIL", SendAttempt.ref_id == n.id))
    assert att.status == "UNCERTAIN"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "notification.reconciled", AuditLog.entity_id == n.id)) is not None
    assert MockEmailProvider.outbox == []  # nothing re-sent
    # Only UNCERTAIN notifications can be reconciled.
    r = client.post(f"/admin/pilot/reconcile/notification/{n.id}", data={"delivered": 1, "csrf": csrf_of(client)})
    assert r.status_code == 404


def test_uncertain_notification_requeued_when_not_delivered(client, db, world):
    n = _uncertain_notification(db, world)
    login(client, "admin@agency.example")
    client.post(f"/admin/pilot/reconcile/notification/{n.id}", data={"delivered": 0, "csrf": csrf_of(client)})
    db.expire_all()
    assert db.get(DealerNotification, n.id).status == "PENDING"
    assert db.scalar(select(Job).where(Job.kind == "customer.notify_email", Job.status == "pending")) is not None
    run_worker()
    db.expire_all()
    assert db.get(DealerNotification, n.id).status == "SIMULATED"
    assert [m.to for m, _ in MockEmailProvider.outbox] == ["admin@a.example"]


def test_notification_reconcile_is_agency_admin_only(client, db, world):
    n = _uncertain_notification(db, world)
    login(client, "manager@agency.example")
    r = client.post(f"/admin/pilot/reconcile/notification/{n.id}", data={"delivered": 1, "csrf": csrf_of(client)})
    assert r.status_code == 403
    db.expire_all()
    assert db.get(DealerNotification, n.id).status == "UNCERTAIN"

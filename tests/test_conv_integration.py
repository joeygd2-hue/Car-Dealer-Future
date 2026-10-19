"""Behaviour that exists only because Phase 3 now runs inside the unified app: shared gates, worker scheduling,
retention, audit, the staff-side HTTP workflow and fixes found while integrating."""

from datetime import timedelta

from sqlalchemy import func, select, update

from dealerflow.db import session_scope, utcnow
from dealerflow.models import (
    AgentState,
    Appointment,
    AuditLog,
    Conversation,
    CustomerContact,
    Escalation,
    IntegrationConnection,
    Job,
    Message,
    Notification,
    ResponseDraft,
    SandboxRecord,
)
from dealerflow.services.conversations import appointments as appt_svc
from dealerflow.services.conversations import notifications, runtime
from tests.conv_helpers import chat, enable_mode_b, login, run_worker


def _last_draft(db, cid):
    return db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid).order_by(ResponseDraft.id.desc()))


def test_mode_b_still_needs_the_phase4_auto_response_gate(db, world):
    from dealerflow.models import AutomationSetting

    enable_mode_b(db, world)
    db.execute(update(AutomationSetting).values(enabled=False))
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "gate-1", "How much is the Civic?")
    run_worker()
    d = _last_draft(db, cid)
    assert d.decision == "DRAFT_ONLY" and any("inquiry_auto_response" in r for r in d.decision_reasons)
    assert db.scalar(select(func.count(Message.id)).where(Message.conversation_id == cid, Message.direction == "OUTBOUND")) == 0


def test_platform_emergency_stop_blocks_auto_replies_and_staff_email(db, world):
    enable_mode_b(db, world)
    db.get(AgentState, 1).status = "emergency_stopped"
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "stop-1", "How much is the Civic?")
    run_worker()
    assert any("emergency stop" in r for r in _last_draft(db, cid).decision_reasons)
    notifications.notify(db, world["a"]["d"].id, kind="ESCALATION", subject="stop-test", body="b", escalation_reasons=["complaint"])
    db.commit()
    run_worker()
    emails = list(db.scalars(select(Notification).where(Notification.subject == "stop-test", Notification.channel == "EMAIL")))
    assert emails and all(n.status == "BLOCKED" for n in emails)
    assert db.scalar(select(SandboxRecord).where(SandboxRecord.kind == "EMAIL_OUT")) is None


def test_global_pause_from_pilot_console_blocks_auto_send(client, db, world):
    enable_mode_b(db, world)
    csrf = login(client, "ops@platform.example")
    assert client.post("/conversations/pilot/pause", data={"on": 1, "reason": "drill", "csrf": csrf},
                       follow_redirects=False).status_code == 303
    _, cid, _ = chat(world["a"]["d"], "pause-1", "How much is the Civic?")
    run_worker()
    assert any("global" in r for r in _last_draft(db, cid).decision_reasons)
    assert runtime.get_flag(db, runtime.GLOBAL_AUTOMATION_PAUSE) is True


def test_both_reminders_are_queued_without_a_key_collision(db, world):
    """Regression: the 24h and 2h reminders for one appointment used the same message idempotency key, so the
    second reminder job failed on every retry."""
    db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="calendar", provider="GOOGLE_CALENDAR", status="sandbox"))
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "rem-both", "I'd like to test drive the Civic Saturday at 11am. I'm Ana, 281-555-0101")
    run_worker()
    appt = db.scalar(select(Appointment).where(Appointment.conversation_id == cid))
    appt_svc.confirm(db, appt, user_id=world["a"]["staff"].id, start_utc=utcnow() + timedelta(days=3))
    db.commit()
    db.execute(update(Job).where(Job.kind == "appointments.send_reminder").values(run_after=utcnow()))
    db.commit()
    run_worker()
    jobs = list(db.scalars(select(Job).where(Job.kind == "appointments.send_reminder")))
    assert len(jobs) == 2 and all(j.status == "done" for j in jobs), [(j.status, j.last_error) for j in jobs]
    reminders = [m for m in db.scalars(select(Message).where(Message.conversation_id == cid, Message.sender_type == "SYSTEM"))
                 if m.body.startswith("Reminder")]
    assert len(reminders) == 2 and all(m.delivery_status == "SIMULATED" for m in reminders), [(m.delivery_status, m.delivery_error) for m in reminders]


def test_worker_schedules_digest_and_reminder_sweep_hourly(db):
    from dealerflow.worker import schedule_tick

    now = utcnow().replace(minute=5)
    schedule_tick(now)
    schedule_tick(now + timedelta(minutes=20))  # same hour → no duplicates
    kinds = [j.kind for j in db.scalars(select(Job).where(Job.kind.in_(("notification.daily_digests", "appointments.dispatch_reminders"))))]
    assert sorted(kinds) == ["appointments.dispatch_reminders", "notification.daily_digests"]
    schedule_tick(now + timedelta(hours=1))
    assert db.scalar(select(func.count(Job.id)).where(Job.kind == "notification.daily_digests")) == 2


def test_conversation_audit_entries_carry_the_client(db, world):
    _, cid, _ = chat(world["a"]["d"], "audit-1", "Is the Civic available?")
    run_worker()
    rows = list(db.scalars(select(AuditLog).where(AuditLog.action.like("conversation.%") | AuditLog.action.like("message.%")
                                                  | AuditLog.action.like("draft.%"))))
    assert rows and all((r.data or {}).get("client_id") == world["a"]["d"].id for r in rows)


def test_retention_purges_customer_text_everywhere_but_keeps_opt_outs(db, world):
    from dealerflow.models import ContactSuppression
    from dealerflow.services.maintenance import purge_conversation_pii

    _, cid, _ = chat(world["a"]["d"], "purge-1", "Do you finance? I'm Ana, ana@example.com, 281-555-0101")
    run_worker()
    chat(world["a"]["d"], "purge-1", "STOP")
    run_worker()
    old = utcnow() - timedelta(days=400)
    db.execute(update(Conversation).values(created_at=old))
    db.execute(update(Notification).values(created_at=old))
    db.execute(update(SandboxRecord).values(created_at=old))
    db.commit()
    assert purge_conversation_pii(db, 365) == 1
    db.commit()
    db.expire_all()
    assert all(m.body == "[purged]" for m in db.scalars(select(Message).where(Message.conversation_id == cid)))
    assert all(d.body == "[purged]" for d in db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == cid)))
    assert all(e.summary == "[purged]" for e in db.scalars(select(Escalation).where(Escalation.conversation_id == cid)))
    assert all(n.body == "[purged]" for n in db.scalars(select(Notification)))
    contact = db.get(CustomerContact, db.get(Conversation, cid).contact_id)
    assert contact.email is None and contact.phone is None and contact.display_name is None
    assert db.get(Conversation, cid).opted_out
    assert db.scalar(select(ContactSuppression).where(ContactSuppression.address == "web:purge-1")) is not None


def test_staff_http_workflow_lead_to_confirmed_test_drive(client, db, world):
    """Website chat → draft → staff send (SIMULATED) → escalation resolved → test drive confirmed → summary emailed
    (SIMULATED) to this dealership only."""
    from dealerflow.integrations.email import MockEmailProvider

    d = world["a"]["d"]
    key = d.public_chat_key
    r = client.post(f"/api/chat/{key}/messages", json={"visitor_id": "journey-visitor", "client_message_id": "j1",
                                                       "text": "Is the Civic available? Can I test drive it Saturday at 11am? I'm Ana, 281-555-0101"})
    assert r.status_code == 200
    run_worker()
    csrf = login(client, "staff@a.example")
    convs = client.get(f"/api/v1/dealers/{d.id}/conversations").json()
    assert len(convs) == 1
    cid = convs[0]["id"]
    full = client.get(f"/api/v1/dealers/{d.id}/conversations/{cid}").json()
    draft = next(x for x in full["drafts"] if x["status"] == "PENDING_REVIEW")
    assert full["appointments"] and full["appointments"][0]["status"] == "PENDING_CONFIRMATION"
    assert "isn't confirmed" in draft["body"]  # a request is never promised
    assert client.post(f"/dealers/{d.id}/conversations/{cid}/send", data={"body": draft["body"], "draft_id": draft["id"],
                                                                         "csrf": csrf}, follow_redirects=False).status_code == 303
    run_worker()
    chat(d, "journey-visitor", "Also, do you offer financing?", msg_id="j2-direct")
    run_worker()
    full = client.get(f"/api/v1/dealers/{d.id}/conversations/{cid}").json()
    esc = next(e for e in full["escalations"] if e["status"] == "OPEN")
    assert "financing" in esc["reasons"]
    assert client.post(f"/dealers/{d.id}/escalations/{esc['id']}/resolve", data={"csrf": csrf},
                       follow_redirects=False).status_code == 303
    appt_id = full["appointments"][0]["id"]
    r = client.post(f"/dealers/{d.id}/appointments/{appt_id}/confirm", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    run_worker()
    full = client.get(f"/api/v1/dealers/{d.id}/conversations/{cid}").json()
    assert full["appointments"][0]["status"] == "CONFIRMED"
    assert all(m["delivery_status"] == "SIMULATED" for m in full["messages"] if m["direction"] == "OUTBOUND")
    assert any("confirmed" in m["body"] for m in full["messages"] if m["direction"] == "OUTBOUND")
    assert client.get(f"/api/chat/{key}/messages", params={"visitor_id": "journey-visitor"}).json() == {"messages": []}
    assert client.post(f"/dealers/{d.id}/conversations/{cid}/summary", data={"csrf": csrf},
                       follow_redirects=False).status_code == 303
    assert [m.to for m, _ in MockEmailProvider.outbox] == ["owner@a.example"]
    with session_scope() as s:
        assert s.get(Conversation, cid).summary_delivered_at is not None
        assert s.get(Conversation, cid).first_response_at is not None


def test_lead_summary_for_a_simulated_dealership_never_uses_a_real_provider(db, world, monkeypatch):
    """Demo/fixture dealerships (``Client.simulated``) must never get real email, even with every live flag on."""
    from dealerflow.config import get_settings
    from dealerflow.models import Client
    from dealerflow.services import client_email
    from dealerflow.services.conversations.summaries import deliver_lead_summary

    c = db.get(Client, world["a"]["d"].id)
    c.simulated = True
    db.add(IntegrationConnection(client_id=c.id, kind="report_email", provider="smtp", status="connected", authorization_note="test"))
    db.commit()
    monkeypatch.setattr(get_settings(), "email_provider", "smtp")
    monkeypatch.setattr(get_settings(), "client_email_sending_enabled", True)
    monkeypatch.setattr(client_email, "get_email_provider", lambda: (_ for _ in ()).throw(AssertionError("must not send")))
    _, cid, _ = chat(c, "sim-dealer", "Is the Civic available?")
    run_worker()
    outcome = deliver_lead_summary(db, db.get(Conversation, cid))
    assert outcome.startswith("not_sent") and "SIMULATED dealership" in outcome

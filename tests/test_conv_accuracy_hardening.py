"""Accuracy guarantees on the FINAL outgoing message, freshness policy, ambiguity, cancellations, opt-outs."""

from datetime import timedelta

from sqlalchemy import select

from dealerflow.db import utcnow
from dealerflow.models import (
    Appointment,
    Conversation,
    Escalation,
    IntegrationConnection,
    Message,
    ResponseDraft,
    Vehicle,
)
from dealerflow.services.conversations import appointments as appt_svc
from dealerflow.services.conversations.processing import process_message
from tests.conv_helpers import chat, enable_mode_b, login, run_worker


def _queue_auto_reply(db, world, visitor, text="How much is the Civic?", setup=True):
    if setup:
        enable_mode_b(db, world)
    _, cid, _ = chat(world["a"]["d"], visitor, text)
    inbound = db.scalar(select(Message).where(Message.conversation_id == cid))
    out = process_message(db, inbound.id)  # draft + queued AI_AUTO message, not yet sent
    db.commit()
    assert out["auto_send"]
    return cid


def test_final_message_blocked_when_price_changes_before_send(db, world):
    cid = _queue_auto_reply(db, world, "fin-1")
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.price_cents = 1799500  # price dropped after the draft was approved by policy
    db.commit()
    run_worker()
    db.expire_all()
    msg = db.scalar(select(Message).where(Message.conversation_id == cid, Message.sender_type == "AI_AUTO"))
    assert msg.delivery_status == "BLOCKED" and "changed" in msg.delivery_error
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    assert draft.status == "PENDING_REVIEW"  # handed back to staff
    assert db.scalar(select(Escalation).where(Escalation.conversation_id == cid)) is not None


def test_final_message_blocked_when_vehicle_sold_or_stale(db, world):
    cid = _queue_auto_reply(db, world, "fin-2", "Is the Civic still available?")
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.inventory_status = "sold"
    db.commit()
    run_worker()
    db.expire_all()
    msg = db.scalar(select(Message).where(Message.conversation_id == cid, Message.sender_type == "AI_AUTO"))
    assert msg.delivery_status == "BLOCKED"
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.inventory_status = "available"
    db.commit()
    cid2 = _queue_auto_reply(db, world, "fin-3", "How much is the Civic?", setup=False)
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.inventory_status, v.verified_at = "available", utcnow() - timedelta(days=3)
    db.commit()
    run_worker()
    db.expire_all()
    msg2 = db.scalar(select(Message).where(Message.conversation_id == cid2, Message.sender_type == "AI_AUTO"))
    assert msg2.delivery_status == "BLOCKED"


def test_unchanged_facts_send_normally(db, world):
    cid = _queue_auto_reply(db, world, "fin-4")
    run_worker()
    db.expire_all()
    msg = db.scalar(select(Message).where(Message.conversation_id == cid, Message.sender_type == "AI_AUTO"))
    assert msg.delivery_status == "SIMULATED" and "$18,995" in msg.body


def test_staff_message_with_unverified_claim_needs_acknowledgement(client, db, world):
    _, cid, _ = chat(world["a"]["d"], "staff-check", "How much is the Civic?")
    run_worker()
    csrf = login(client, "staff@a.example")
    url = f"/dealers/{world['a']['d'].id}/conversations/{cid}/send"
    r = client.post(url, data={"body": "Great news, it has no accidents and I can do $15,000 today!", "csrf": csrf})
    assert r.status_code == 200 and "Not sent" in r.text
    assert db.scalar(select(Message).where(Message.conversation_id == cid, Message.direction == "OUTBOUND")) is None
    r = client.post(url, data={"body": "Great news, it has no accidents and I can do $15,000 today!", "csrf": csrf,
                               "acknowledge_warnings": "on"}, follow_redirects=False)
    assert r.status_code == 303
    from dealerflow.models import AuditLog

    assert db.scalar(select(AuditLog).where(AuditLog.action == "message.staff_override")) is not None
    ok = client.post(url, data={"body": "The listed price is $18,995.", "csrf": csrf}, follow_redirects=False)
    assert ok.status_code == 303  # verified content needs no acknowledgement


def test_ambiguous_vehicle_escalates(db, world):
    from dealerflow.models import Client
    from dealerflow.services.conversations.seeding import add_vehicle

    add_vehicle(db, db.get(Client, world["a"]["d"].id), stock_number="A101", year=2020, make="Honda", model="Civic", trim="LX",
                price_cents=2099500, mileage=20000, verified_at=utcnow())
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "amb-1", "How much is the Honda Civic?")
    run_worker()
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    assert "ambiguous_vehicle" in draft.escalation_reasons and "$" not in draft.body


def test_stale_mileage_not_stated(db, world):
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.verified_at = utcnow() - timedelta(days=2)
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "mi-1", "How many miles on the Civic?")
    run_worker()
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    assert "45,210" not in draft.body and draft.escalation_reasons


def test_customer_cancellation_flags_appointment_for_staff(db, world):
    _, cid, _ = chat(world["a"]["d"], "cx-1", "I'd like to test drive the Civic Saturday at 11am. I'm Ana, 281-555-0101")
    run_worker()
    appt = db.scalar(select(Appointment).where(Appointment.conversation_id == cid))
    appt_svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    db.commit()
    chat(world["a"]["d"], "cx-1", "Sorry, I need to cancel my appointment")
    run_worker()
    db.expire_all()
    appt = db.get(Appointment, appt.id)
    assert appt.status == "RESCHEDULE_REQUESTED"  # flagged, never auto-cancelled
    assert "CUSTOMER_CHANGE_REQUESTED" in [e.event for e in appt.events]
    esc = db.scalar(select(Escalation).where(Escalation.conversation_id == cid))
    assert "appointment_change" in esc.reasons


def test_spanish_cancellation_and_reschedule(db, world):
    _, cid, _ = chat(world["a"]["d"], "cx-es", "Quiero una prueba de manejo del Civic el sábado a las 11. Soy Luis, 832-555-0102")
    run_worker()
    appt = db.scalar(select(Appointment).where(Appointment.conversation_id == cid))
    assert appt is not None and db.get(Conversation, cid).preferred_language == "es"
    chat(world["a"]["d"], "cx-es", "No voy a poder ir, quiero cambiar mi cita")
    run_worker()
    db.expire_all()
    assert db.get(Appointment, appt.id).status == "RESCHEDULE_REQUESTED"
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid).order_by(ResponseDraft.id.desc()))
    assert draft.language == "es" and "cita" in draft.body


def test_opt_out_blocks_pending_reminder(db, world):
    db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="calendar", provider="GOOGLE_CALENDAR", status="sandbox",
                                 external_account_id="cal"))
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "rem-1", "Can I test drive the Civic Saturday at 11am? This is Kim, 281-555-0103")
    run_worker()
    appt = db.scalar(select(Appointment).where(Appointment.conversation_id == cid))
    appt_svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()  # confirmation goes out
    chat(world["a"]["d"], "rem-1", "ALTO")
    run_worker()
    appt_svc._customer_message(db, db.get(Appointment, appt.id), "reminder")  # reminder fires after the opt-out
    db.commit()
    run_worker()
    db.expire_all()
    reminders = [m for m in db.scalars(select(Message).where(Message.conversation_id == cid, Message.sender_type == "SYSTEM"))
                 if "Reminder" in m.body or "Recordatorio" in m.body]
    assert reminders == []  # never even queued after the opt-out …
    events = [(e.event, e.details) for e in db.get(Appointment, appt.id).events]
    assert ("REMINDER_NOT_SENT", {"reason": "opted out"}) in events  # … and the reason is recorded
    assert db.get(Conversation, cid).opted_out


def test_reminder_for_cancelled_appointment_blocked_at_send(db, world):
    _, cid, _ = chat(world["a"]["d"], "rem-2", "Can I test drive the Civic Saturday at 11am? This is Lee, 281-555-0104")
    run_worker()
    appt = db.scalar(select(Appointment).where(Appointment.conversation_id == cid))
    appt_svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    appt_svc._customer_message(db, appt, "reminder")  # queued while confirmed …
    appt.status = "CANCELLED"                          # … then cancelled before the worker sends it
    db.commit()
    run_worker()
    db.expire_all()
    rem = next(m for m in db.scalars(select(Message).where(Message.conversation_id == cid, Message.sender_type == "SYSTEM"))
               if m.body.startswith("Reminder"))
    assert rem.delivery_status == "BLOCKED" and "appointment changed" in rem.delivery_error

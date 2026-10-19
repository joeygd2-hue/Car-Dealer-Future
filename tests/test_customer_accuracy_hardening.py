"""Accuracy guarantees on the FINAL outgoing message, freshness policy, ambiguity, cancellations, opt-outs.

Ported from app/tests/test_accuracy_hardening.py. Mapping notes: old ``verified_at`` -> ``last_seen_at``;
old Vehicle.status "SOLD"/"AVAILABLE" -> ``inventory_status`` "sold"/"available"; old DEALER_STAFF user ->
dealer_reviewer; old portal send URL -> /dealers/{id}/inbox/{conv}/send (form field ``csrf``).
``enable_mode_b`` is a local copy of the old helper from test_automation_modes.py, adapted to dealerflow (which
additionally requires the Phase 4 ``inquiry_auto_response`` automation switch for Mode B/C)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dealerflow.db import utcnow
from dealerflow.models import AuditLog
from dealerflow.models_customer import (
    MODE_FAQ,
    ApprovedResponseTemplate,
    CustomerConversation,
    CustomerMessage,
    Escalation,
    EvaluationRun,
    ModeAuthorization,
    ResponseAutomationSettings,
    ResponseDraft,
    TestDriveAppointment,
)
from dealerflow.models_dealer import Vehicle
from dealerflow.models_ops import AutomationSetting
from dealerflow.services.customer import appointments as appt_svc
from dealerflow.services.customer.policy import ONBOARDING_FEATURE
from dealerflow.services.customer.processing import process_message
from dealerflow.services.responder.engine import ENGINE_VERSION
from tests.customer_helpers import add_vehicle, chat, dealer_login, run_worker, world  # noqa: F401


def enable_mode_b(db, world, key="a"):
    cid, admin = world[key]["id"], world[key]["admin"]
    run = EvaluationRun(client_id=cid, engine_version=ENGINE_VERSION, total_cases=1, metrics={}, passed=True)
    db.add(run)
    db.flush()
    db.add(ModeAuthorization(client_id=cid, mode=MODE_FAQ, statement="test", evaluation_run_id=run.id, authorized_by_id=admin))
    db.add(AutomationSetting(client_id=cid, feature=ONBOARDING_FEATURE, enabled=True, enabled_by_id=admin, enabled_at=utcnow()))
    s = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == cid))
    s.mode = MODE_FAQ
    for intent, body in (("price", "The listed price for the {vehicle} is {price}."),
                         ("availability", "The {vehicle} is currently available."),
                         ("greeting", "Hi{name}! Thanks for contacting {dealer_name}.")):
        db.add(ApprovedResponseTemplate(client_id=cid, intent=intent, language="en", body=body, auto_send_allowed=True,
                                        approved_by_id=admin, approved_at=utcnow()))
    db.commit()


def _queue_auto_reply(db, world, visitor, text="How much is the Civic?", setup=True):
    if setup:
        enable_mode_b(db, world)
    _, cid, _ = chat(world["a"]["id"], visitor, text)
    inbound = db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == cid))
    out = process_message(db, inbound.id)  # draft + queued AI_AUTO message, not yet sent
    db.commit()
    assert out["auto_send"]
    return cid


def test_final_message_blocked_when_price_changes_before_send(db, world):
    cid = _queue_auto_reply(db, world, "fin-1")
    v = db.get(Vehicle, world["a"]["civic"])
    v.price_cents = 1799500  # price dropped after the draft was approved by policy
    db.commit()
    run_worker()
    db.expire_all()
    msg = db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == cid, CustomerMessage.sender_type == "AI_AUTO"))
    assert msg.delivery_status == "BLOCKED" and "changed" in msg.delivery_error
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    assert draft.status == "PENDING_REVIEW"  # handed back to staff
    assert db.scalar(select(Escalation).where(Escalation.conversation_id == cid)) is not None


def test_final_message_blocked_when_vehicle_sold_or_stale(db, world):
    cid = _queue_auto_reply(db, world, "fin-2", "Is the Civic still available?")
    v = db.get(Vehicle, world["a"]["civic"])
    v.inventory_status = "sold"
    db.commit()
    run_worker()
    db.expire_all()
    msg = db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == cid, CustomerMessage.sender_type == "AI_AUTO"))
    assert msg.delivery_status == "BLOCKED"
    v = db.get(Vehicle, world["a"]["civic"])
    v.inventory_status = "available"
    db.commit()
    cid2 = _queue_auto_reply(db, world, "fin-3", "How much is the Civic?", setup=False)
    v = db.get(Vehicle, world["a"]["civic"])
    v.inventory_status, v.last_seen_at = "available", utcnow() - timedelta(days=3)
    db.commit()
    run_worker()
    db.expire_all()
    msg2 = db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == cid2, CustomerMessage.sender_type == "AI_AUTO"))
    assert msg2.delivery_status == "BLOCKED"


def test_unchanged_facts_send_normally(db, world):
    cid = _queue_auto_reply(db, world, "fin-4")
    run_worker()
    db.expire_all()
    msg = db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == cid, CustomerMessage.sender_type == "AI_AUTO"))
    assert msg.delivery_status == "SIMULATED" and "$18,995" in msg.body


def test_staff_message_with_unverified_claim_needs_acknowledgement(client, db, world):
    _, cid, _ = chat(world["a"]["id"], "staff-check", "How much is the Civic?")
    run_worker()
    csrf = dealer_login(client, "reviewer@a.example")
    url = f"/dealers/{world['a']['id']}/inbox/{cid}/send"
    r = client.post(url, data={"body": "Great news, it has no accidents and I can do $15,000 today!", "csrf": csrf})
    assert r.status_code == 200 and "Not sent" in r.text
    assert db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == cid, CustomerMessage.direction == "OUTBOUND")) is None
    r = client.post(url, data={"body": "Great news, it has no accidents and I can do $15,000 today!", "csrf": csrf,
                               "acknowledge_warnings": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.scalar(select(AuditLog).where(AuditLog.action == "message.staff_override")) is not None
    ok = client.post(url, data={"body": "The listed price is $18,995.", "csrf": csrf}, follow_redirects=False)
    assert ok.status_code == 303  # verified content needs no acknowledgement


def test_ambiguous_vehicle_escalates(db, world):
    add_vehicle(db, world["a"]["id"], stock="A101", year=2020, make="Honda", model="Civic", trim="LX", price_cents=2099500,
                mileage=20000, status="available", features=[], seen_hours_ago=0)
    db.commit()
    _, cid, _ = chat(world["a"]["id"], "amb-1", "How much is the Honda Civic?")
    run_worker()
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    assert "ambiguous_vehicle" in draft.escalation_reasons and "$" not in draft.body


def test_stale_mileage_not_stated(db, world):
    v = db.get(Vehicle, world["a"]["civic"])
    v.last_seen_at = utcnow() - timedelta(days=2)
    db.commit()
    _, cid, _ = chat(world["a"]["id"], "mi-1", "How many miles on the Civic?")
    run_worker()
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    assert "45,210" not in draft.body and draft.escalation_reasons


def test_customer_cancellation_flags_appointment_for_staff(db, world):
    _, cid, _ = chat(world["a"]["id"], "cx-1", "I'd like to test drive the Civic Saturday at 11am. I'm Ana, 281-555-0101")
    run_worker()
    appt = db.scalar(select(TestDriveAppointment).where(TestDriveAppointment.conversation_id == cid))
    appt_svc.confirm(db, appt, user_id=world["a"]["reviewer"])
    db.commit()
    chat(world["a"]["id"], "cx-1", "Sorry, I need to cancel my appointment")
    run_worker()
    db.expire_all()
    appt = db.get(TestDriveAppointment, appt.id)
    assert appt.status == "RESCHEDULE_REQUESTED"  # flagged, never auto-cancelled
    assert "CUSTOMER_CHANGE_REQUESTED" in [e.event for e in appt.events]
    esc = db.scalar(select(Escalation).where(Escalation.conversation_id == cid))
    assert "appointment_change" in esc.reasons


def test_spanish_cancellation_and_reschedule(db, world):
    _, cid, _ = chat(world["a"]["id"], "cx-es", "Quiero una prueba de manejo del Civic el sábado a las 11. Soy Luis, 832-555-0102")
    run_worker()
    appt = db.scalar(select(TestDriveAppointment).where(TestDriveAppointment.conversation_id == cid))
    assert appt is not None and db.get(CustomerConversation, cid).preferred_language == "es"
    chat(world["a"]["id"], "cx-es", "No voy a poder ir, quiero cambiar mi cita")
    run_worker()
    db.expire_all()
    assert db.get(TestDriveAppointment, appt.id).status == "RESCHEDULE_REQUESTED"
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid).order_by(ResponseDraft.id.desc()))
    assert draft.language == "es" and "cita" in draft.body


def test_opt_out_blocks_pending_reminder(db, world):
    # The old test also added a GOOGLE_CALENDAR connection; dealerflow has no dealership test-drive calendar
    # (appointments.dealership_calendar() always returns None), so there is nothing to connect.
    _, cid, _ = chat(world["a"]["id"], "rem-1", "Can I test drive the Civic Saturday at 11am? This is Kim, 281-555-0103")
    run_worker()
    appt = db.scalar(select(TestDriveAppointment).where(TestDriveAppointment.conversation_id == cid))
    appt_svc.confirm(db, appt, user_id=world["a"]["reviewer"])
    db.commit()
    run_worker()  # confirmation goes out
    chat(world["a"]["id"], "rem-1", "ALTO")
    run_worker()
    appt_svc._customer_message(db, db.get(TestDriveAppointment, appt.id), "reminder")  # reminder fires after the opt-out
    db.commit()
    run_worker()
    db.expire_all()
    reminders = [m for m in db.scalars(select(CustomerMessage).where(CustomerMessage.conversation_id == cid,
                                                                     CustomerMessage.sender_type == "SYSTEM"))
                 if "Reminder" in m.body or "Recordatorio" in m.body]
    assert reminders == []  # never even queued after the opt-out …
    events = [(e.event, e.details) for e in db.get(TestDriveAppointment, appt.id).events]
    assert ("REMINDER_NOT_SENT", {"reason": "opted out"}) in events  # … and the reason is recorded
    assert db.get(CustomerConversation, cid).opted_out


def test_reminder_for_cancelled_appointment_blocked_at_send(db, world):
    _, cid, _ = chat(world["a"]["id"], "rem-2", "Can I test drive the Civic Saturday at 11am? This is Lee, 281-555-0104")
    run_worker()
    appt = db.scalar(select(TestDriveAppointment).where(TestDriveAppointment.conversation_id == cid))
    appt_svc.confirm(db, appt, user_id=world["a"]["reviewer"])
    appt_svc._customer_message(db, appt, "reminder")  # queued while confirmed …
    appt.status = "CANCELLED"                          # … then cancelled before the worker sends it
    db.commit()
    run_worker()
    db.expire_all()
    rem = next(m for m in db.scalars(select(CustomerMessage).where(CustomerMessage.conversation_id == cid,
                                                                   CustomerMessage.sender_type == "SYSTEM"))
               if m.body.startswith("Reminder"))
    assert rem.delivery_status == "BLOCKED" and "appointment changed" in rem.delivery_error

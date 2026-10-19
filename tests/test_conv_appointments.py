"""Phase 3 test-drive appointments: staff confirmation, customer messages, reminders, changes and opt-out."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dealerflow.db import session_scope, utcnow
from dealerflow.models import Appointment, AppointmentEvent, ConversationMessage, Job
from tests.conv_helpers import chat, only_conversation, outbound, setup_dealer
from tests.dealer_helpers import drain


def _requested(client, d):
    chat(client, d["key"], "Can I test drive the Camry next Saturday at 11am? 281-555-0101", name="Ana")
    drain()
    with session_scope() as db:
        appt = db.scalar(select(Appointment).where(Appointment.client_id == d["id"]))
        db.expunge(appt)
    return appt


def test_confirm_queues_confirmation_and_reminders(client):
    d = setup_dealer()
    appt = _requested(client, d)
    assert appt.status == "pending_confirmation"
    start = (utcnow() + timedelta(days=3)).replace(microsecond=0)
    r = client.post(f"/api/v1/dealers/{d['id']}/appointments/{appt.id}/confirm", headers=d["h"]["reviewer"],
                    json={"start_utc": start.isoformat()})
    assert r.json()["status"] == "confirmed"
    drain()
    conv = only_conversation(d["id"])
    system = [m for m in outbound(conv.id) if m.sender_type == "system"]  # drain() also makes the reminders due
    [msg] = [m for m in system if "is confirmed for" in m.body]
    assert msg.delivery_status == "simulated" and all(m.delivery_status == "simulated" for m in system)
    with session_scope() as db:
        reminders = list(db.scalars(select(Job).where(Job.kind == "appointment_reminder")))
        assert len(reminders) == 2  # 24 h and 2 h before
        events = [e.event for e in db.scalars(select(AppointmentEvent).where(AppointmentEvent.appointment_id == appt.id))]
        assert events.index("confirmed") < events.index("confirmation_queued")
    # Confirming twice is refused.
    assert client.post(f"/api/v1/dealers/{d['id']}/appointments/{appt.id}/confirm", headers=d["h"]["reviewer"],
                       json={}).status_code == 400


def test_reminder_is_skipped_when_the_appointment_changed(client):
    from dealerflow.services.conversations.appointments import reminder_job

    d = setup_dealer()
    appt = _requested(client, d)
    start = (utcnow() + timedelta(days=3)).replace(microsecond=0)
    client.post(f"/api/v1/dealers/{d['id']}/appointments/{appt.id}/confirm", headers=d["h"]["reviewer"],
                json={"start_utc": start.isoformat()})
    client.post(f"/api/v1/dealers/{d['id']}/appointments/{appt.id}/cancel", headers=d["h"]["reviewer"], json={"note": "car sold"})
    drain()
    with session_scope() as db:
        assert reminder_job(db, {"appointment_id": appt.id, "start": start.isoformat()}) == {"skipped": "appointment changed"}
        kinds = [m.body for m in db.scalars(select(ConversationMessage).where(ConversationMessage.sender_type == "system"))]
        assert any("cancelled" in b for b in kinds)


def test_customer_change_request_flags_without_cancelling(client):
    d = setup_dealer()
    appt = _requested(client, d)
    chat(client, d["key"], "I need to cancel my appointment", msg_id="later")
    drain()
    with session_scope() as db:
        a = db.get(Appointment, appt.id)
        assert a.status == "reschedule_requested"  # staff decide; nothing is cancelled automatically


def test_opted_out_customer_gets_no_appointment_messages(client):
    d = setup_dealer()
    appt = _requested(client, d)
    chat(client, d["key"], "STOP", msg_id="stop")
    drain()
    start = (utcnow() + timedelta(days=3)).replace(microsecond=0)
    client.post(f"/api/v1/dealers/{d['id']}/appointments/{appt.id}/confirm", headers=d["h"]["reviewer"],
                json={"start_utc": start.isoformat()})
    drain()
    conv = only_conversation(d["id"])
    assert [m for m in outbound(conv.id) if m.sender_type == "system"] == []
    with session_scope() as db:
        assert db.scalar(select(AppointmentEvent).where(AppointmentEvent.event == "confirmation_not_sent"))


def test_viewer_cannot_confirm(client):
    d = setup_dealer()
    appt = _requested(client, d)
    r = client.post(f"/api/v1/dealers/{d['id']}/appointments/{appt.id}/confirm", headers=d["h"]["viewer"], json={})
    assert r.status_code == 403

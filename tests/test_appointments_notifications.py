"""Test-drive booking lifecycle and notification delivery status."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db import utcnow
from app.integrations.base import IntegrationError
from app.models import (
    Appointment,
    IntegrationConnection,
    Job,
    Message,
    Notification,
    NotificationPreference,
    ResponseDraft,
    SandboxRecord,
)
from app.services import appointments as svc
from tests.conftest import chat, run_worker


def _request(db, world, visitor="t-1"):
    _, cid, _ = chat(world["a"]["d"], visitor, "I'd like to test drive the Civic Saturday at 11am. I'm John, 281-555-0199")
    run_worker()
    return cid, db.scalar(select(Appointment).where(Appointment.conversation_id == cid))


def test_request_recorded_but_never_promised(db, world):
    cid, appt = _request(db, world)
    assert appt.status == "PENDING_CONFIRMATION" and appt.availability_checked == "NO_CALENDAR"
    assert appt.customer_name == "John" and appt.customer_phone == "2815550199"
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    assert "isn't confirmed" in draft.body
    assert draft.decision == "DRAFT_ONLY"
    assert db.scalar(select(Notification).where(Notification.kind == "APPOINTMENT")) is not None


def test_confirm_with_calendar_creates_event_confirmation_and_reminders(db, world):
    db.add(IntegrationConnection(dealership_id=world["a"]["d"].id, provider="GOOGLE_CALENDAR", external_account_id="cal-a",
                                 status="SANDBOX"))
    db.commit()
    cid, appt = _request(db, world, "t-2")
    assert appt.availability_checked == "FREE"
    svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    db.commit()
    assert appt.status == "CONFIRMED" and appt.calendar_event_id.startswith("sandbox-evt")
    reminders = list(db.scalars(select(Job).where(Job.type == "appointments.send_reminder")))
    assert len(reminders) >= 1
    run_worker()
    conf = db.scalar(select(Message).where(Message.conversation_id == cid, Message.sender_type == "SYSTEM"))
    assert conf is not None and "confirmed" in conf.body and conf.delivery_status == "SIMULATED"  # chat not live-authorized
    # Calendar now busy at that time → a second request for the same slot is flagged BUSY.
    _, appt2 = _request(db, world, "t-3")
    assert appt2.availability_checked == "BUSY"


def test_calendar_failure_keeps_appointment_unconfirmed(db, world):
    db.add(IntegrationConnection(dealership_id=world["a"]["d"].id, provider="GOOGLE_CALENDAR", external_account_id="cal-a",
                                 status="SANDBOX", config={"sandbox_failure_mode": "retryable"}))
    db.commit()
    _, appt = _request(db, world, "t-4")
    assert appt.availability_checked == "CHECK_FAILED"
    with pytest.raises(IntegrationError):
        svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    db.rollback()
    assert db.get(Appointment, appt.id).status == "PENDING_CONFIRMATION"


def test_reschedule_and_cancel_tracked(db, world):
    db.add(IntegrationConnection(dealership_id=world["a"]["d"].id, provider="GOOGLE_CALENDAR", external_account_id="cal-a",
                                 status="SANDBOX"))
    db.commit()
    _, appt = _request(db, world, "t-5")
    svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    old_event = appt.calendar_event_id
    svc.reschedule(db, appt, appt.requested_start + timedelta(hours=2), user_id=world["a"]["staff"].id)
    assert appt.status == "RESCHEDULE_REQUESTED" and appt.calendar_event_id is None
    rec = next(r for r in db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "CAL_EVENT")) if r.payload["id"] == old_event)
    assert rec.payload.get("cancelled")
    svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    svc.cancel(db, appt, actor="user", user_id=world["a"]["staff"].id, reason="customer asked")
    db.commit()
    events = [e.event for e in appt.events]
    assert {"REQUESTED", "CONFIRMED", "RESCHEDULED", "CANCELLED"} <= set(events)
    # Stale reminder for the original time is skipped.
    db.execute(update(Job).where(Job.type == "appointments.send_reminder").values(run_at=utcnow()))
    db.commit()
    run_worker()
    assert all(j.status in ("SUCCEEDED", "CANCELLED") for j in db.scalars(select(Job).where(Job.type == "appointments.send_reminder")))


def test_outside_hours_request_not_created(db, world):
    _, cid, _ = chat(world["a"]["d"], "t-6", "Can I come see the Civic Sunday at 2pm?")
    run_worker()
    assert db.scalar(select(Appointment).where(Appointment.conversation_id == cid)) is None
    assert "outside our business hours" in db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid)).body


def test_notification_statuses_simulated_failed_skipped(db, world):
    from app.services import notifications

    notifications.notify(db, world["a"]["d"].id, kind="ESCALATION", subject="s", body="b", escalation_reasons=["complaint"])
    db.commit()
    run_worker()
    emails = list(db.scalars(select(Notification).where(Notification.channel == "EMAIL")))
    assert emails and all(n.status == "SIMULATED" and "not actually sent" in n.status_detail for n in emails)
    inapp = list(db.scalars(select(Notification).where(Notification.channel == "IN_APP")))
    assert inapp and all(n.status == "DELIVERED" for n in inapp)
    # Failure path: notification email provider failing permanently.
    db.add(IntegrationConnection(dealership_id=None, provider="NOTIFY_EMAIL", external_account_id="platform-smtp",
                                 config={"sandbox_failure_mode": "permanent"}))
    db.commit()
    notifications.notify(db, world["a"]["d"].id, kind="ESCALATION", subject="s2", body="b", dedupe_key="x")
    db.commit()
    run_worker()
    failed = list(db.scalars(select(Notification).where(Notification.subject == "s2", Notification.channel == "EMAIL")))
    assert failed and all(n.status == "FAILED" for n in failed)
    # Preference off → SKIPPED (shown honestly, not "sent").
    prefs = db.scalar(select(NotificationPreference).where(NotificationPreference.dealership_id == world["a"]["d"].id))
    prefs.email_enabled = False
    db.commit()
    notifications.notify(db, world["a"]["d"].id, kind="NEW_INQUIRY", subject="s3", body="b")
    db.commit()
    assert all(n.status == "SKIPPED" for n in db.scalars(select(Notification).where(
        Notification.subject == "s3", Notification.channel == "EMAIL")))


def test_daily_digest(db, world, monkeypatch):
    from app.services import notifications

    prefs = notifications.get_prefs(db, world["a"]["d"].id)
    from zoneinfo import ZoneInfo
    prefs.digest_hour_local = utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Chicago")).hour
    db.commit()
    notifications.daily_digests_job(db, {}, None)
    notifications.daily_digests_job(db, {}, None)  # idempotent within the day
    db.commit()
    digests = list(db.scalars(select(Notification).where(Notification.kind == "DIGEST", Notification.channel == "IN_APP")))
    assert len(digests) == 1  # one per dealership admin, once per day despite two runs
    assert "Daily summary" in digests[0].body

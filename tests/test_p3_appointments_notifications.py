"""Phase 3 test-drive booking lifecycle (sandbox calendar) and notification delivery status."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from dealerflow.db import utcnow
from dealerflow.models import IntegrationConnection, IntegrationLog, Job
from dealerflow.models_conversations import Appointment, Notification, NotificationPreference, ResponseDraft
from dealerflow.models_conversations import ConversationMessage as Message
from dealerflow.services.conversations import appointments as svc
from dealerflow.services.conversations.channels import CalendarError
from tests.phase3 import build_world, chat, run_due, run_worker

REMINDER = "conversations.appointment_reminder"


@pytest.fixture
def world(db):
    return build_world(db)


def _calendar(db, world, **config):
    db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="calendar", provider="sandbox_calendar",
                                 external_account_id="cal-a", status="sandbox", config=config))
    db.commit()


def _request(db, world, visitor="t-1"):
    _, cid, _ = chat(world["a"]["d"], visitor, "I'd like to test drive the Civic Saturday at 11am. I'm John, 281-555-0199")
    run_due()
    db.expire_all()
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
    _calendar(db, world)
    cid, appt = _request(db, world, "t-2")
    assert appt.availability_checked == "FREE"
    svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    db.commit()
    assert appt.status == "CONFIRMED" and appt.calendar_event_id.startswith("sandbox-evt")
    reminders = list(db.scalars(select(Job).where(Job.kind == REMINDER)))
    assert len(reminders) >= 1
    run_due()
    db.expire_all()
    conf = db.scalar(select(Message).where(Message.conversation_id == cid, Message.sender_type == "SYSTEM"))
    assert conf is not None and "confirmed" in conf.body and conf.delivery_status == "SIMULATED"
    assert db.scalar(select(IntegrationLog).where(IntegrationLog.action == "create_event", IntegrationLog.simulated.is_(True)))
    # The calendar is now busy at that time: a second request for the same slot is flagged BUSY.
    _, appt2 = _request(db, world, "t-3")
    assert appt2.availability_checked == "BUSY"


def test_calendar_failure_keeps_appointment_unconfirmed(db, world):
    _calendar(db, world, sandbox_failure_mode="retryable")
    _, appt = _request(db, world, "t-4")
    assert appt.availability_checked == "CHECK_FAILED"
    with pytest.raises(CalendarError):
        svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    db.rollback()
    assert db.get(Appointment, appt.id).status == "PENDING_CONFIRMATION"


def test_reschedule_and_cancel_tracked_and_stale_reminders_skipped(db, world):
    _calendar(db, world)
    _, appt = _request(db, world, "t-5")
    svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    svc.reschedule(db, appt, appt.requested_start + timedelta(hours=2), user_id=world["a"]["staff"].id)
    assert appt.status == "RESCHEDULE_REQUESTED" and appt.calendar_event_id is None
    assert db.scalar(select(IntegrationLog).where(IntegrationLog.action == "cancel_event"))
    svc.confirm(db, appt, user_id=world["a"]["staff"].id)
    svc.cancel(db, appt, actor="user", user_id=world["a"]["staff"].id, reason="customer asked")
    db.commit()
    events = [e.event for e in appt.events]
    assert {"REQUESTED", "CONFIRMED", "RESCHEDULED", "CANCELLED"} <= set(events)
    # Reminders for the cancelled booking run but send nothing.
    db.execute(update(Job).where(Job.kind == REMINDER).values(run_after=utcnow()))
    db.commit()
    run_due()
    db.expire_all()
    jobs = list(db.scalars(select(Job).where(Job.kind == REMINDER)))
    assert jobs and all(j.status == "done" and j.result.get("skipped") for j in jobs)
    assert db.scalar(select(Message).where(Message.conversation_id == appt.conversation_id,
                                           Message.body.contains("reminder"))) is None


def test_two_reminders_for_one_booking_do_not_collide(db, world):
    """Regression: the 24h and 2h reminders used to share one idempotency key."""
    _, appt = _request(db, world, "t-7")
    svc.confirm(db, appt, user_id=world["a"]["staff"].id, start_utc=utcnow() + timedelta(days=3))
    db.commit()
    db.execute(update(Job).where(Job.kind == REMINDER).values(run_after=utcnow()))
    db.commit()
    run_due()
    db.expire_all()
    jobs = list(db.scalars(select(Job).where(Job.kind == REMINDER)))
    assert len(jobs) == 2 and {j.status for j in jobs} == {"done"}
    reminders = list(db.scalars(select(Message).where(Message.conversation_id == appt.conversation_id,
                                                      Message.sender_type == "SYSTEM", Message.idempotency_key.contains("reminder"))))
    assert len(reminders) == 2


def test_outside_hours_request_not_created(db, world):
    _, cid, _ = chat(world["a"]["d"], "t-6", "Can I come see the Civic Sunday at 2pm?")
    run_worker()
    assert db.scalar(select(Appointment).where(Appointment.conversation_id == cid)) is None
    assert "outside our business hours" in db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid)).body


def test_notification_statuses_simulated_failed_skipped(db, world, monkeypatch):
    from dealerflow.integrations.email import MockEmailProvider
    from dealerflow.services.conversations import notifications

    notifications.notify(db, world["a"]["d"].id, kind="ESCALATION", subject="s", body="b", escalation_reasons=["complaint"])
    db.commit()
    run_worker()
    db.expire_all()
    emails = list(db.scalars(select(Notification).where(Notification.channel == "EMAIL")))
    assert emails and all(n.status == "SIMULATED" and "NOT actually sent" in n.status_detail for n in emails)
    inapp = list(db.scalars(select(Notification).where(Notification.channel == "IN_APP")))
    assert inapp and all(n.status == "DELIVERED" for n in inapp)

    def boom(self, msg):
        raise RuntimeError("provider rejected the message")

    monkeypatch.setattr(MockEmailProvider, "send", boom)
    notifications.notify(db, world["a"]["d"].id, kind="ESCALATION", subject="s2", body="b", dedupe_key="x")
    db.commit()
    run_worker()
    db.expire_all()
    failed = list(db.scalars(select(Notification).where(Notification.subject == "s2", Notification.channel == "EMAIL")))
    assert failed and all(n.status == "FAILED" for n in failed)
    prefs = db.scalar(select(NotificationPreference).where(NotificationPreference.client_id == world["a"]["d"].id))
    prefs.email_enabled = False
    db.commit()
    notifications.notify(db, world["a"]["d"].id, kind="NEW_INQUIRY", subject="s3", body="b")
    db.commit()
    assert all(n.status == "SKIPPED" for n in db.scalars(select(Notification).where(
        Notification.subject == "s3", Notification.channel == "EMAIL")))


def test_daily_digest(db, world):
    from zoneinfo import ZoneInfo

    from dealerflow.services.conversations import notifications

    prefs = notifications.get_prefs(db, world["a"]["d"].id)
    prefs.digest_hour_local = utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Chicago")).hour
    db.commit()
    notifications.daily_digests_job(db, {})
    notifications.daily_digests_job(db, {})  # idempotent within the day
    db.commit()
    digests = list(db.scalars(select(Notification).where(Notification.kind == "DIGEST", Notification.channel == "IN_APP",
                                                         Notification.client_id == world["a"]["d"].id)))
    assert len(digests) == 1
    assert "Daily summary" in digests[0].body

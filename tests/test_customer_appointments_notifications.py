"""Test-drive booking lifecycle and notification delivery status.

Ported from app/tests/test_appointments_notifications.py. dealerflow has no per-dealership calendar integration
(``appointments.dealership_calendar`` always returns None, availability is NO_CALENDAR and staff confirm by hand),
so the calendar-event / BUSY / calendar-failure parts of the old tests have no equivalent and are left out.
Notification email goes through ``client_email`` with the SIMULATED mock provider. Synthetic data only.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from dealerflow.db import utcnow
from dealerflow.integrations.email import MockEmailProvider
from dealerflow.integrations.errors import PermanentError
from dealerflow.models import Job
from dealerflow.models_customer import (
    CustomerMessage,
    DealerNotification,
    DealerNotificationPreference,
    ResponseDraft,
    TestDriveAppointment,
)
from dealerflow.models_dealer import Client
from dealerflow.services.customer import appointments as svc
from dealerflow.services.customer import notify as notifications
from tests.customer_helpers import chat, run_worker, world  # noqa: F401


def _request(db, world, visitor="t-1"):
    _, cid, _ = chat(world["a"]["id"], visitor, "I'd like to test drive the Civic Saturday at 11am. I'm John, 281-555-0199")
    run_worker()
    db.expire_all()
    return cid, db.scalar(select(TestDriveAppointment).where(TestDriveAppointment.conversation_id == cid))


def test_request_recorded_but_never_promised(db, world):
    cid, appt = _request(db, world)
    assert appt.status == "PENDING_CONFIRMATION" and appt.availability_checked == "NO_CALENDAR"
    assert appt.customer_name == "John" and appt.customer_phone == "2815550199"
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    assert "isn't confirmed" in draft.body
    assert draft.decision == "DRAFT_ONLY"
    assert db.scalar(select(DealerNotification).where(DealerNotification.kind == "APPOINTMENT")) is not None


def test_confirm_creates_confirmation_and_reminders(db, world):
    """Old: test_confirm_with_calendar_creates_event_confirmation_and_reminders (calendar parts have no equivalent)."""
    cid, appt = _request(db, world, "t-2")
    svc.confirm(db, appt, user_id=world["a"]["reviewer"])
    db.commit()
    assert appt.status == "CONFIRMED" and appt.confirmed_start == appt.requested_start
    reminders = list(db.scalars(select(Job).where(Job.kind == "customer.appt_reminder")))
    assert len(reminders) >= 1
    run_worker()
    db.expire_all()
    conf = db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == cid, CustomerMessage.sender_type == "SYSTEM"))
    assert conf is not None and "confirmed" in conf.body and conf.delivery_status == "SIMULATED"  # chat not live-authorized


def test_reschedule_and_cancel_tracked(db, world):
    _, appt = _request(db, world, "t-5")
    uid = world["a"]["reviewer"]
    svc.confirm(db, appt, user_id=uid)
    svc.reschedule(db, appt, appt.requested_start + timedelta(hours=2), user_id=uid)
    assert appt.status == "RESCHEDULE_REQUESTED" and appt.calendar_event_id is None and appt.confirmed_start is None
    svc.confirm(db, appt, user_id=uid)
    svc.cancel(db, appt, actor="user", user_id=uid, reason="customer asked")
    db.commit()
    events = [e.event for e in appt.events]
    assert {"REQUESTED", "CONFIRMED", "RESCHEDULED", "CANCELLED"} <= set(events)
    # Stale reminders (for the original and the rescheduled time) are skipped.
    db.execute(update(Job).where(Job.kind == "customer.appt_reminder").values(run_after=utcnow()))
    db.commit()
    run_worker()
    db.expire_all()
    reminders = list(db.scalars(select(Job).where(Job.kind == "customer.appt_reminder")))
    assert reminders and all(j.status == "done" and (j.result or {}).get("skipped") for j in reminders), \
        [(j.key, j.status, j.result) for j in reminders]


def test_outside_hours_request_not_created(db, world):
    _, cid, _ = chat(world["a"]["id"], "t-6", "Can I come see the Civic Sunday at 2pm?")
    run_worker()
    db.expire_all()
    assert db.scalar(select(TestDriveAppointment).where(TestDriveAppointment.conversation_id == cid)) is None
    assert "outside our business hours" in db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid)).body


def test_notification_statuses_simulated_failed_skipped(db, world):
    a = world["a"]["id"]
    notifications.notify(db, a, kind="ESCALATION", subject="s", body="b", escalation_reasons=["complaint"])
    db.commit()
    run_worker()
    db.expire_all()
    emails = list(db.scalars(select(DealerNotification).where(DealerNotification.channel == "EMAIL")))
    assert emails and all(n.status == "SIMULATED" and "nothing was emailed" in n.status_detail for n in emails)
    inapp = list(db.scalars(select(DealerNotification).where(DealerNotification.channel == "IN_APP")))
    assert inapp and all(n.status == "DELIVERED" for n in inapp)
    # Failure path: the notification email provider rejects the message permanently.
    MockEmailProvider.fail_next = [PermanentError("simulated permanent provider error (HTTP 400)")]
    notifications.notify(db, a, kind="ESCALATION", subject="s2", body="b", dedupe_key="x")
    db.commit()
    run_worker()
    db.expire_all()
    failed = list(db.scalars(select(DealerNotification).where(DealerNotification.subject == "s2", DealerNotification.channel == "EMAIL")))
    assert failed and all(n.status == "FAILED" for n in failed), [(n.status, n.status_detail) for n in failed]
    # Preference off → SKIPPED (shown honestly, not "sent").
    prefs = db.scalar(select(DealerNotificationPreference).where(DealerNotificationPreference.client_id == a))
    prefs.email_enabled = False
    db.commit()
    notifications.notify(db, a, kind="NEW_INQUIRY", subject="s3", body="b")
    db.commit()
    s3 = list(db.scalars(select(DealerNotification).where(DealerNotification.subject == "s3", DealerNotification.channel == "EMAIL")))
    assert s3 and all(n.status == "SKIPPED" for n in s3)


def test_daily_digest(db, world):
    a = world["a"]["id"]
    prefs = notifications.get_prefs(db, a)
    tz = db.get(Client, a).timezone
    prefs.digest_hour_local = utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).hour
    db.commit()
    notifications.daily_digests_job(db, {})
    notifications.daily_digests_job(db, {})  # idempotent within the day
    db.commit()
    digests = list(db.scalars(select(DealerNotification).where(DealerNotification.client_id == a, DealerNotification.kind == "DIGEST",
                                                               DealerNotification.channel == "IN_APP")))
    assert len(digests) == 1  # one per dealership admin, once per day despite two runs
    assert "Daily summary" in digests[0].body

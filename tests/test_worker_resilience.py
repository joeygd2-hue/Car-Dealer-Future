"""Worker crash/restart/race/outage behaviour of the send pipelines."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app import db as dbmod
from app.db import utcnow
from app.integrations import sandbox
from app.models import Conversation, IntegrationConnection, Job, Message, OutreachMessage, SandboxRecord, SendAttempt
from app.services import controls, jobs, rate_limits, send_gate
from app.services.inbox.outbound import queue_outbound, reconcile_uncertain
from app.worker import Worker
from tests.conftest import chat, run_worker


class Crash(BaseException):
    """Simulates the process dying (not an Exception, so nothing in the worker catches it)."""


def _conv(db, world, visitor):
    _, cid, _ = chat(world["a"]["d"], visitor, "Is the Civic available?")
    run_worker()
    return db.get(Conversation, cid)


def _channel_out(db):
    return db.scalar(select(func.count(SandboxRecord.id)).where(SandboxRecord.kind == "CHANNEL_OUT"))


def _expire_leases(db):
    """Simulate the lease period passing: job leases expire and in-flight attempts age past the lease."""
    db.execute(update(Job).where(Job.status.in_(["RUNNING", "QUEUED"]))
               .values(locked_until=utcnow() - timedelta(seconds=1), run_at=utcnow()))
    db.execute(update(SendAttempt).where(SendAttempt.status == "IN_FLIGHT")
               .values(created_at=utcnow() - timedelta(seconds=600)))
    db.commit()


def test_crash_after_provider_call_never_resends(db, world, monkeypatch):
    conv = _conv(db, world, "crash-1")
    msg = queue_outbound(db, conv, "Hello from staff", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    real = sandbox.SandboxCustomerChannel.send_text

    def accept_then_die(self, recipient, text, human_agent=False):
        real(self, recipient, text, human_agent=human_agent)
        self.db.commit()  # the provider has the message …
        raise Crash()     # … and the worker dies before recording the result

    monkeypatch.setattr(sandbox.SandboxCustomerChannel, "send_text", accept_then_die)
    with pytest.raises(Crash):
        Worker(worker_id="doomed", poll_seconds=0).run_once()
    monkeypatch.setattr(sandbox.SandboxCustomerChannel, "send_text", real)
    sent_before = _channel_out(db)
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SENDING"
    assert db.scalar(select(SendAttempt).where(SendAttempt.kind == "CUSTOMER_MESSAGE", SendAttempt.ref_id == msg.id)).status == "IN_FLIGHT"
    _expire_leases(db)
    Worker(worker_id="after-restart", poll_seconds=0).run_once()
    db.expire_all()
    m = db.get(Message, msg.id)
    assert m.delivery_status == "UNCERTAIN" and _channel_out(db) == sent_before  # not re-sent
    assert db.scalar(select(SendAttempt).where(SendAttempt.kind == "CUSTOMER_MESSAGE", SendAttempt.ref_id == msg.id)).status == "UNCERTAIN"
    # Operator reconciles: confirmed not delivered → re-queued once and sent.
    reconcile_uncertain(db, m, delivered=False, user_id=world["platform"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SIMULATED" and _channel_out(db) == sent_before + 1


def test_provider_timeout_marks_uncertain_without_retry(db, world):
    conv = _conv(db, world, "timeout-1")
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.dealership_id == world["a"]["d"].id,
                                                         IntegrationConnection.provider == "WEBSITE_CHAT"))
    conv.channel, conv.external_conversation_id = "TWILIO_SMS", "+18325550001"
    db.add(IntegrationConnection(dealership_id=world["a"]["d"].id, provider="TWILIO_SMS", external_account_id="+12815550199",
                                 config={"sandbox_failure_mode": "uncertain"}))
    db.commit()
    msg = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "UNCERTAIN"
    job = db.scalar(select(Job).where(Job.type == "outbound.send", Job.idempotency_key == f"outbound:{msg.id}"))
    assert job.status == "SUCCEEDED" and job.attempts == 1  # no automatic retry
    assert conn is not None


def test_two_workers_cannot_send_the_same_outreach_message(db, world):
    from app.services.outreach import agent

    st = agent.get_state(db)
    st.send_window_start_hour, st.send_window_end_hour, st.send_weekdays = 0, 24, list(range(7))
    from app.models import Campaign

    db.add(Campaign(name="T", status="ACTIVE", content_approved_at=utcnow(), daily_send_limit=50))
    agent.start(db, user_id=world["platform"].id)
    db.commit()
    from tests.test_outreach_agent import tick_all

    tick_all(db, 3)
    msg = db.scalar(select(OutreachMessage).where(OutreachMessage.status == "PENDING_APPROVAL"))
    agent.approve_message(db, msg, user_id=world["platform"].id)
    for i in range(4):  # four send jobs for one message (e.g. duplicate enqueues across run generations)
        jobs.enqueue(db, "outreach.send", {"message_id": msg.id}, idempotency_key=f"race-{i}")
    db.commit()
    errors = []

    def work(n):
        # Like run_forever: a transient DB error ("database is locked") ends the iteration, the loop tries again.
        w = Worker(worker_id=f"racer-{n}", poll_seconds=0)
        for _ in range(20):
            try:
                w.run_once()
                return
            except OperationalError as exc:
                errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    db.expire_all()
    sent = [r for r in db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "EMAIL_OUT"))
            if r.payload["to"] == msg.to_address]
    assert len(sent) == 1, errors
    assert db.get(OutreachMessage, msg.id).status == "SIMULATED"


def test_distributed_counter_never_exceeds_limit_under_concurrency(db):
    import time

    wins, failures = [], []

    def grab():
        s = dbmod.SessionLocal()
        deadline = time.monotonic() + 20
        try:
            while True:
                try:
                    ok = rate_limits.try_acquire(s, "race:key", 7, 3600)
                    s.commit()
                    wins.append(ok)
                    return
                except OperationalError as exc:  # SQLite "database is locked" under contention → retry
                    s.rollback()
                    if time.monotonic() > deadline:
                        failures.append(repr(exc))
                        return
                    time.sleep(0.01)
        finally:
            s.close()

    threads = [threading.Thread(target=grab) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not failures, failures
    assert len(wins) == 20 and sum(wins) == 7 and rate_limits.current(db, "race:key", 3600) == 7


def test_worker_survives_database_outage(db, monkeypatch):
    calls = {"n": 0}
    real_claim = jobs.claim_next

    def flaky_claim(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OperationalError("SELECT", {}, Exception("database unavailable"))
        return real_claim(*a, **k)

    monkeypatch.setattr(jobs, "claim_next", flaky_claim)
    from tests.test_jobs import CALLS

    CALLS["n"] = 0
    job = jobs.enqueue(db, "test.flaky", {"succeed_on": 1})
    db.commit()
    w = Worker(worker_id="resilient", poll_seconds=0)
    loops = {"n": 0}
    real_once = w.run_once

    def counted_once(*a, **k):
        loops["n"] += 1
        if loops["n"] >= 5:
            w.stop()
        return real_once(*a, **k)

    w.run_once = counted_once
    w.run_forever()  # must not die on the two failed iterations
    db.expire_all()
    assert db.get(Job, job.id).status == "SUCCEEDED" and calls["n"] >= 3


def test_global_pause_and_emergency_stop_survive_restart(db, world):
    conv = _conv(db, world, "restart-1")
    controls.set_flag(db, send_gate.EMERGENCY_STOP_ALL, True, user_id=None, reason="test")
    db.commit()
    staff = queue_outbound(db, conv, "staff note", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    Worker(worker_id="new-process", poll_seconds=0).run_once()  # brand-new worker, no memory of the flag
    db.expire_all()
    m = db.get(Message, staff.id)
    assert m.delivery_status == "BLOCKED" and "emergency stop" in m.delivery_error
    att = db.scalar(select(SendAttempt).where(SendAttempt.kind == "CUSTOMER_MESSAGE", SendAttempt.ref_id == staff.id))
    assert att.status == "BLOCKED"  # the blocked attempt is in the ledger


def test_outreach_pause_holds_messages_across_restart(db, world):
    from app.models import Campaign
    from app.services.outreach import agent
    from tests.test_outreach_agent import tick_all

    st = agent.get_state(db)
    st.send_window_start_hour, st.send_window_end_hour, st.send_weekdays = 0, 24, list(range(7))
    db.add(Campaign(name="T", status="ACTIVE", content_approved_at=utcnow(), daily_send_limit=50))
    agent.start(db, user_id=world["platform"].id)
    db.commit()
    tick_all(db, 3)
    msg = db.scalar(select(OutreachMessage).where(OutreachMessage.status == "PENDING_APPROVAL"))
    agent.approve_message(db, msg, user_id=world["platform"].id)
    agent.pause(db, user_id=world["platform"].id, reason="test")
    jobs.enqueue(db, "outreach.send", {"message_id": msg.id}, idempotency_key="paused-send")
    db.commit()
    Worker(worker_id="fresh", poll_seconds=0).run_once()
    db.expire_all()
    assert db.get(OutreachMessage, msg.id).status == "APPROVED"
    assert db.scalar(select(Job).where(Job.idempotency_key == "paused-send")).status == "QUEUED"  # deferred, not lost


def test_duplicate_job_does_not_mark_a_live_send_uncertain(db, world):
    """Regression (found by repeated clean-clone runs): a second job that finds a message SENDING must not
    declare it UNCERTAIN while the claiming worker is still within its lease (it may be mid-send)."""
    conv = _conv(db, world, "live-owner-1")
    msg = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    # Worker A has claimed and opened an attempt moments ago (still mid-send).
    db.execute(update(Message).where(Message.id == msg.id).values(delivery_status="SENDING"))
    db.add(SendAttempt(kind="CUSTOMER_MESSAGE", ref_id=msg.id, channel="WEBSITE_CHAT", recipient="web:x",
                       live=False, status="IN_FLIGHT", reasons=[]))
    db.commit()
    jobs.enqueue(db, "outbound.send", {"message_id": msg.id}, idempotency_key="duplicate-job")
    db.execute(update(Job).where(Job.status == "QUEUED").values(run_at=utcnow()))
    db.commit()
    Worker(worker_id="worker-b", poll_seconds=0).run_once()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SENDING"  # untouched: worker A owns it
    deferred = db.scalar(select(Job).where(Job.idempotency_key == "duplicate-job"))
    assert deferred.status == "QUEUED" and deferred.attempts == 0 and "another worker" in deferred.last_error

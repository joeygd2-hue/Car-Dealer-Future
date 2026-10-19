"""Worker crash/restart/race behaviour of the customer send pipeline, ported from Phase 3.

The Phase 3 outreach-agent variants were not ported: the unified app has one outreach engine (dealerflow's) with its
own tests. Every send here is SIMULATED (sandbox adapter); "provider" means the sandbox recorder.
"""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from dealerflow.db import session_factory, utcnow
from dealerflow.jobs import STALE_LOCK, enqueue, run_pending
from dealerflow.models import AgentState, Conversation, IntegrationConnection, Job, Message, SandboxRecord, SendAttempt
from dealerflow.services.conversations import channels, rate_limits, send_gate
from dealerflow.services.conversations.outbound import queue_outbound, reconcile_uncertain
from tests.conv_helpers import chat, run_worker


class Crash(BaseException):
    """Simulates the process dying (not an Exception, so nothing in the worker catches it)."""


def _conv(db, world, visitor):
    _, cid, _ = chat(world["a"]["d"], visitor, "Is the Civic available?")
    run_worker()
    return db.get(Conversation, cid)


def _channel_out(db):
    return db.scalar(select(func.count(SandboxRecord.id)).where(SandboxRecord.kind == "CHANNEL_OUT"))


def _attempt(db, msg_id):
    return db.scalar(select(SendAttempt).where(SendAttempt.kind == "CUSTOMER_MESSAGE", SendAttempt.ref_id == msg_id)
                     .order_by(SendAttempt.id.desc()))


def _expire_leases(db):
    """Simulate the lease period passing: running jobs look abandoned and in-flight attempts age past the lease."""
    db.execute(update(Job).where(Job.status == "running").values(locked_at=utcnow() - STALE_LOCK - timedelta(seconds=1)))
    db.execute(update(Job).where(Job.status == "pending").values(run_after=utcnow()))
    db.execute(update(SendAttempt).where(SendAttempt.status == "IN_FLIGHT")
               .values(created_at=utcnow() - STALE_LOCK - timedelta(seconds=1)))
    db.commit()


def test_crash_after_provider_call_never_resends(db, world, monkeypatch):
    conv = _conv(db, world, "crash-1")
    msg = queue_outbound(db, conv, "Hello from staff", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    real = channels.SandboxCustomerChannel.send_text

    def accept_then_die(self, recipient, text, human_agent=False):
        real(self, recipient, text, human_agent=human_agent)
        self.db.commit()  # the provider has the message …
        raise Crash()     # … and the worker dies before recording the result

    monkeypatch.setattr(channels.SandboxCustomerChannel, "send_text", accept_then_die)
    with pytest.raises(Crash):
        run_pending()
    monkeypatch.setattr(channels.SandboxCustomerChannel, "send_text", real)
    sent_before = _channel_out(db)
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SENDING" and _attempt(db, msg.id).status == "IN_FLIGHT"
    _expire_leases(db)
    run_pending()  # a restarted worker
    db.expire_all()
    m = db.get(Message, msg.id)
    assert m.delivery_status == "UNCERTAIN" and _channel_out(db) == sent_before  # not re-sent
    assert _attempt(db, msg.id).status == "UNCERTAIN"
    # Operator reconciles: confirmed not delivered → re-queued once and sent.
    reconcile_uncertain(db, m, delivered=False, user_id=world["platform"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SIMULATED" and _channel_out(db) == sent_before + 1


def test_provider_timeout_marks_uncertain_without_retry(db, world):
    conv = _conv(db, world, "timeout-1")
    conv.channel, conv.external_conversation_id = "TWILIO_SMS", "+18325550001"
    db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="TWILIO_SMS", status="sandbox",
                                 external_account_id="+12815550199", config={"sandbox_failure_mode": "uncertain"}))
    db.commit()
    msg = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "UNCERTAIN"
    job = db.scalar(select(Job).where(Job.kind == "outbound.send", Job.key == f"outbound:{msg.id}"))
    assert job.status == "done" and job.attempts == 1  # no automatic retry


def test_distributed_counter_never_exceeds_limit_under_concurrency(db):
    import time

    wins, failures = [], []
    factory = session_factory()

    def grab():
        s = factory()
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


def test_emergency_stop_survives_restart(db, world):
    conv = _conv(db, world, "restart-1")
    st = db.get(AgentState, 1)
    st.status = "emergency_stopped"  # the platform emergency stop (Agent page) is stored in the database
    db.commit()
    staff = queue_outbound(db, conv, "staff note", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_pending()  # brand-new worker run, no memory of the flag
    db.expire_all()
    m = db.get(Message, staff.id)
    assert m.delivery_status == "BLOCKED" and "emergency stop" in m.delivery_error
    assert _attempt(db, staff.id).status == "BLOCKED"  # the blocked attempt is in the ledger


def test_duplicate_job_does_not_mark_a_live_send_uncertain(db, world):
    """A second job that finds a message SENDING must not declare it UNCERTAIN while the claiming worker is still
    within its lease (it may be mid-send)."""
    conv = _conv(db, world, "live-owner-1")
    msg = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    # Worker A has claimed and opened an attempt moments ago (still mid-send).
    db.execute(update(Message).where(Message.id == msg.id).values(delivery_status="SENDING"))
    db.add(SendAttempt(kind="CUSTOMER_MESSAGE", ref_id=msg.id, client_id=conv.client_id, channel="WEBSITE_CHAT", recipient="web:x",
                       live=False, status="IN_FLIGHT", reasons=[]))
    db.commit()
    enqueue(db, "outbound.send", "duplicate-job", {"message_id": msg.id})
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SENDING"  # untouched: worker A owns it
    deferred = db.scalar(select(Job).where(Job.key == "duplicate-job"))
    assert deferred.status == "pending" and deferred.attempts == 0 and "another worker" in deferred.last_error


def test_owner_finishing_between_checks_is_not_overwritten(db, world, monkeypatch):
    """Worker B reads the row as SENDING, worker A then completes the send, and only afterwards does B look for
    A's in-flight attempt. B must not overwrite A's successful outcome with UNCERTAIN."""
    conv = _conv(db, world, "toctou-1")
    row = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    db.execute(update(Message).where(Message.id == row.id).values(delivery_status="SENDING"))
    att = SendAttempt(kind="CUSTOMER_MESSAGE", ref_id=row.id, client_id=conv.client_id, channel="WEBSITE_CHAT", recipient="x",
                      live=False, status="IN_FLIGHT", reasons=[], created_at=utcnow() - STALE_LOCK - timedelta(seconds=5))
    db.add(att)
    db.commit()
    real = send_gate.in_flight_attempt
    factory = session_factory()

    def a_finishes_first(session, k, ref_id):
        found = real(session, k, ref_id)
        with factory() as other:  # worker A completes in its own session *between* B's reads
            other.execute(update(SendAttempt).where(SendAttempt.id == att.id).values(status="SIMULATED"))
            other.execute(update(Message).where(Message.id == row.id).values(delivery_status="SIMULATED"))
            other.commit()
        return found

    monkeypatch.setattr(send_gate, "in_flight_attempt", a_finishes_first)
    enqueue(db, "outbound.send", "toctou-customer", {"message_id": row.id})
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(Message, row.id).delivery_status == "SIMULATED"

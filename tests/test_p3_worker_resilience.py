"""Phase 3 send pipeline under worker crashes, restarts, duplicate jobs and races. A send whose outcome is unknown
is marked UNCERTAIN and never re-sent automatically."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from dealerflow.db import session_factory, session_scope, utcnow
from dealerflow.jobs import enqueue, run_pending
from dealerflow.models import IntegrationConnection, IntegrationLog, Job
from dealerflow.models_conversations import Conversation, SendAttempt
from dealerflow.models_conversations import ConversationMessage as Message
from dealerflow.services.conversations import channels, rate_limits, send_gate
from dealerflow.services.conversations.outbound import queue_outbound, reconcile_uncertain
from tests.phase3 import build_world, chat, run_due, run_worker

SEND = "conversations.outbound_send"


class Crash(BaseException):
    """Simulates the process dying (not an Exception, so nothing in the job runner catches it)."""


@pytest.fixture
def world(db):
    return build_world(db)


def _conv(db, world, visitor):
    _, cid, _ = chat(world["a"]["d"], visitor, "Is the Civic available?")
    run_worker()
    db.expire_all()
    return db.get(Conversation, cid)


def _channel_out(db):
    return db.scalar(select(func.count(IntegrationLog.id)).where(IntegrationLog.action == "send_text",
                                                                 IntegrationLog.status == "ok"))


def _expire_leases(db):
    """Simulate the lease period passing: running jobs become reclaimable and in-flight attempts age past it."""
    db.execute(update(Job).where(Job.status == "running").values(locked_at=utcnow() - timedelta(minutes=16)))
    db.execute(update(SendAttempt).where(SendAttempt.status == "IN_FLIGHT")
               .values(created_at=utcnow() - send_gate.WORKER_LEASE - timedelta(seconds=10)))
    db.commit()


def test_crash_after_provider_call_never_resends(db, world, monkeypatch):
    conv = _conv(db, world, "crash-1")
    msg = queue_outbound(db, conv, "Hello from staff", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    real = channels.SandboxCustomerChannel.send_text

    def accept_then_die(self, recipient, text, *, human_agent=False):
        real(self, recipient, text, human_agent=human_agent)
        self.db.commit()  # the provider has the message ...
        raise Crash()     # ... and the worker dies before recording the result

    monkeypatch.setattr(channels.SandboxCustomerChannel, "send_text", accept_then_die)
    with pytest.raises(Crash):
        run_pending()
    monkeypatch.setattr(channels.SandboxCustomerChannel, "send_text", real)
    sent_before = _channel_out(db)
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SENDING"
    attempt = select(SendAttempt).where(SendAttempt.kind == "CUSTOMER_MESSAGE", SendAttempt.ref_id == msg.id)
    assert db.scalar(attempt).status == "IN_FLIGHT"
    _expire_leases(db)
    run_due()
    db.expire_all()
    m = db.get(Message, msg.id)
    assert m.delivery_status == "UNCERTAIN" and _channel_out(db) == sent_before  # not re-sent
    assert db.scalar(attempt).status == "UNCERTAIN"
    # A person reconciles: confirmed not delivered -> queued once more and sent.
    reconcile_uncertain(db, m, delivered=False, user_id=world["platform"].id)
    db.commit()
    run_due()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SIMULATED" and _channel_out(db) == sent_before + 1


def test_provider_timeout_marks_uncertain_without_retry(db, world):
    conv = _conv(db, world, "timeout-1")
    conv.channel, conv.external_conversation_id = "TWILIO_SMS", "+18325550001"
    db.add(IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="twilio_sms",
                                 external_account_id="+12815550199", status="sandbox", config={"sandbox_failure_mode": "uncertain"}))
    db.commit()
    msg = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_due()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "UNCERTAIN"
    job = db.scalar(select(Job).where(Job.kind == SEND))
    assert job.status == "done" and job.attempts == 1  # no automatic retry


def test_duplicate_job_does_not_mark_a_live_send_uncertain(db, world):
    """A second job that finds a message SENDING must not declare it UNCERTAIN while the claiming worker is still
    within its lease (it may be mid-send)."""
    conv = _conv(db, world, "live-owner-1")
    msg = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    db.execute(update(Message).where(Message.id == msg.id).values(delivery_status="SENDING"))
    db.add(SendAttempt(kind="CUSTOMER_MESSAGE", ref_id=msg.id, client_id=conv.client_id, channel="WEBSITE_CHAT",
                       recipient="web:x", live=False, status="IN_FLIGHT", reasons=[]))
    db.execute(update(Job).where(Job.kind == SEND).values(status="done"))  # worker A's job (it is mid-send)
    db.commit()
    enqueue(db, SEND, "duplicate-job", {"message_id": msg.id})
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "SENDING"  # untouched: worker A owns it
    deferred = db.scalar(select(Job).where(Job.key == "duplicate-job"))
    assert deferred.status == "pending" and deferred.run_after > utcnow() and "another worker" in (deferred.last_error or "")


def test_owner_finishing_between_checks_is_not_overwritten(db, world, monkeypatch):
    """Worker B reads the row as SENDING, worker A then completes the send, and only afterwards does B look for
    A's in-flight attempt. B must not overwrite A's successful outcome with UNCERTAIN."""
    conv = _conv(db, world, "toctou-1")
    row = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    db.execute(update(Message).where(Message.id == row.id).values(delivery_status="SENDING"))
    db.execute(update(Job).where(Job.kind == SEND).values(status="done"))
    att = SendAttempt(kind="CUSTOMER_MESSAGE", ref_id=row.id, client_id=conv.client_id, channel="WEBSITE_CHAT",
                      recipient="x", live=False, status="IN_FLIGHT", reasons=[],
                      created_at=utcnow() - send_gate.WORKER_LEASE - timedelta(seconds=10))  # looks crashed
    db.add(att)
    db.commit()
    real = send_gate.in_flight_attempt

    def a_finishes_first(session, k, ref_id):
        with session_factory()() as other:
            other.execute(update(SendAttempt).where(SendAttempt.id == att.id).values(status="SIMULATED"))
            other.execute(update(Message).where(Message.id == row.id).values(delivery_status="SIMULATED"))
            other.commit()
        return real(session, k, ref_id)

    monkeypatch.setattr(send_gate, "in_flight_attempt", a_finishes_first)
    enqueue(db, SEND, "toctou", {"message_id": row.id})
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(Message, row.id).delivery_status == "SIMULATED"


def test_crashed_owner_with_unfinished_attempt_is_taken_over_once(db, world):
    conv = _conv(db, world, "takeover-1")
    row = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    db.execute(update(Message).where(Message.id == row.id).values(delivery_status="SENDING"))
    db.execute(update(Job).where(Job.kind == SEND).values(status="done"))
    db.add(SendAttempt(kind="CUSTOMER_MESSAGE", ref_id=row.id, client_id=conv.client_id, channel="WEBSITE_CHAT", recipient="x",
                       live=False, status="IN_FLIGHT", reasons=[],
                       created_at=utcnow() - send_gate.WORKER_LEASE - timedelta(seconds=10)))
    db.commit()
    for key in ("dup-1", "dup-2"):
        enqueue(db, SEND, key, {"message_id": row.id})
    db.commit()
    before = _channel_out(db)
    run_pending()
    db.expire_all()
    assert db.get(Message, row.id).delivery_status == "UNCERTAIN" and _channel_out(db) == before


def test_distributed_counter_never_exceeds_limit_under_concurrency(db):
    import time

    wins, failures = [], []

    def grab():
        s = session_factory()()
        deadline = time.monotonic() + 20
        try:
            while True:
                try:
                    ok = rate_limits.try_acquire(s, "race:key", 7, 3600)
                    s.commit()
                    wins.append(ok)
                    return
                except OperationalError as exc:  # SQLite "database is locked" under contention -> retry
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
    assert len(wins) == 20 and sum(wins) == 7
    with session_scope() as s:
        assert rate_limits.current(s, "race:key", 3600) == 7

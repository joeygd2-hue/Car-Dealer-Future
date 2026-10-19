"""Crash / restart / race behaviour of the customer-message send pipeline.

Ported from app/tests/test_worker_resilience.py: only the customer-message sending tests (duplicate-send protection,
crash takeover → UNCERTAIN, ``check_sending_row`` / ``take_over_crashed`` races, emergency stop, the shared
auto-reply rate counter). dealerflow keeps its own job queue (dealerflow/jobs.py): a "worker" is a ``run_pending``
call, a job lock goes stale after ``jobs.STALE_LOCK`` and the platform emergency stop is Phase 1's AgentState.
Sandbox deliveries are recorded in ``integration_log`` (action ``customer_send``). Synthetic data only.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from dealerflow.db import session_factory, utcnow
from dealerflow.integrations import customer_channels
from dealerflow.jobs import STALE_LOCK, enqueue, run_pending
from dealerflow.models import IntegrationLog, Job, User
from dealerflow.models_customer import (
    CustomerConversation,
    CustomerMessage,
    SendAttempt,
)
from dealerflow.models_ops import IntegrationConnection
from dealerflow.services import agent
from dealerflow.services.customer import rate_limits, send_gate
from dealerflow.services.customer.outbound import queue_outbound, reconcile_uncertain
from tests.customer_helpers import chat, run_worker, world  # noqa: F401


class Crash(BaseException):
    """Simulates the process dying (not an Exception, so nothing in the job runner catches it)."""


def _conv(db, world, visitor):
    _, cid, _ = chat(world["a"]["id"], visitor, "Is the Civic available?")
    run_worker()
    db.expire_all()
    return db.get(CustomerConversation, cid)


def _channel_out(db):
    db.expire_all()
    return db.scalar(select(func.count(IntegrationLog.id)).where(IntegrationLog.action == "customer_send",
                                                                 IntegrationLog.status == "ok",
                                                                 IntegrationLog.provider.like("sandbox:%")))


def _expire_leases(db):
    """Simulate the lease period passing: running jobs' locks go stale and in-flight attempts age past the lease."""
    old = utcnow() - STALE_LOCK - timedelta(seconds=1)
    db.execute(update(Job).where(Job.status == "running").values(locked_at=old))
    db.execute(update(Job).where(Job.status.in_(["running", "pending"])).values(run_after=utcnow()))
    db.execute(update(SendAttempt).where(SendAttempt.status == "IN_FLIGHT").values(created_at=old))
    db.commit()


def _attempt(db, msg_id):
    db.expire_all()
    return db.scalar(select(SendAttempt).where(SendAttempt.kind == "CUSTOMER_MESSAGE", SendAttempt.ref_id == msg_id)
                     .order_by(SendAttempt.id))


def test_crash_after_provider_call_never_resends(db, world, monkeypatch):
    conv = _conv(db, world, "crash-1")
    msg = queue_outbound(db, conv, "Hello from staff", sender_type="STAFF", user_id=world["a"]["reviewer"])
    db.commit()
    real = customer_channels.SandboxCustomerChannel.send_text

    def accept_then_die(self, recipient, text, human_agent=False):
        real(self, recipient, text, human_agent=human_agent)
        self.db.commit()  # the provider has the message …
        raise Crash()     # … and the worker dies before recording the result

    monkeypatch.setattr(customer_channels.SandboxCustomerChannel, "send_text", accept_then_die)
    with pytest.raises(Crash):
        run_pending()
    monkeypatch.setattr(customer_channels.SandboxCustomerChannel, "send_text", real)
    sent_before = _channel_out(db)
    db.expire_all()
    assert db.get(CustomerMessage, msg.id).delivery_status == "SENDING"
    assert _attempt(db, msg.id).status == "IN_FLIGHT"
    _expire_leases(db)
    run_pending()  # "after restart"
    db.expire_all()
    m = db.get(CustomerMessage, msg.id)
    assert m.delivery_status == "UNCERTAIN" and _channel_out(db) == sent_before  # not re-sent
    assert _attempt(db, msg.id).status == "UNCERTAIN"
    # Operator reconciles: confirmed not delivered → re-queued once and sent.
    m = db.get(CustomerMessage, msg.id)
    reconcile_uncertain(db, m, delivered=False, user_id=world["staff_admin"])
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(CustomerMessage, msg.id).delivery_status == "SIMULATED" and _channel_out(db) == sent_before + 1


def test_provider_timeout_marks_uncertain_without_retry(db, world):
    conv = _conv(db, world, "timeout-1")
    conv.channel, conv.external_conversation_id = "TWILIO_SMS", "+18325550001"
    db.add(IntegrationConnection(client_id=world["a"]["id"], kind="customer_messaging", provider="twilio_sms",
                                 external_account_id="+12815550199", status="sandbox",
                                 config={"sandbox_failure_mode": "uncertain"}))
    db.commit()
    msg = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["reviewer"])
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(CustomerMessage, msg.id).delivery_status == "UNCERTAIN"
    job = db.scalar(select(Job).where(Job.kind == "customer.send", Job.key == f"customer.send:{msg.id}"))
    assert job.status == "done" and job.attempts == 1  # no automatic retry


def test_distributed_counter_never_exceeds_limit_under_concurrency(db):
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


def test_global_pause_and_emergency_stop_survive_restart(db, world):
    conv = _conv(db, world, "restart-1")
    agent.set_status(db, "emergency_stopped", actor=db.get(User, world["staff_admin"]), reason="test")
    db.commit()
    staff = queue_outbound(db, conv, "staff note", sender_type="STAFF", user_id=world["a"]["reviewer"])
    db.commit()
    run_pending()  # brand-new worker pass, no memory of the flag
    db.expire_all()
    m = db.get(CustomerMessage, staff.id)
    assert m.delivery_status == "BLOCKED" and "emergency stop" in m.delivery_error
    assert _attempt(db, staff.id).status == "BLOCKED"  # the blocked attempt is in the ledger


def test_duplicate_job_does_not_mark_a_live_send_uncertain(db, world):
    """Regression: a second job that finds a message SENDING must not declare it UNCERTAIN while the claiming
    worker is still within its lease (it may be mid-send)."""
    conv = _conv(db, world, "live-owner-1")
    msg = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["reviewer"])
    db.commit()
    # Worker A has claimed and opened an attempt moments ago (still mid-send).
    db.execute(update(CustomerMessage).where(CustomerMessage.id == msg.id).values(delivery_status="SENDING"))
    db.add(SendAttempt(kind="CUSTOMER_MESSAGE", ref_id=msg.id, client_id=world["a"]["id"], channel="WEBSITE_CHAT",
                       recipient="web:x", live=False, status="IN_FLIGHT", reasons=[]))
    db.commit()
    enqueue(db, "customer.send", "duplicate-job", {"message_id": msg.id})
    db.execute(update(Job).where(Job.status == "pending").values(run_after=utcnow()))
    db.commit()
    run_pending()  # worker B
    db.expire_all()
    assert db.get(CustomerMessage, msg.id).delivery_status == "SENDING"  # untouched: worker A owns it
    deferred = db.scalar(select(Job).where(Job.key == "duplicate-job"))
    assert deferred.status == "pending" and deferred.attempts == 0 and "another worker" in deferred.last_error


def test_owner_finishing_between_checks_is_not_overwritten(db, world, monkeypatch):
    """Deterministic reproduction of the race: worker B reads the row as SENDING, worker A then completes the send,
    and only afterwards does B look for A's in-flight attempt. B must not overwrite A's outcome with UNCERTAIN.
    (Old test was parametrized customer/outreach; only the customer case is in scope here.)"""
    conv = _conv(db, world, "toctou-1")
    row = queue_outbound(db, conv, "Hi", sender_type="STAFF", user_id=world["a"]["reviewer"])
    db.commit()
    # Worker A has claimed the row and opened its in-flight attempt.
    db.execute(update(CustomerMessage).where(CustomerMessage.id == row.id).values(delivery_status="SENDING"))
    att = SendAttempt(kind="CUSTOMER_MESSAGE", ref_id=row.id, client_id=world["a"]["id"], channel="WEBSITE_CHAT",
                      recipient="x", live=False, status="IN_FLIGHT", reasons=[])
    db.add(att)
    db.commit()
    att_id, row_id = att.id, row.id
    real = send_gate.in_flight_attempt

    def a_finishes_first(session, k, ref_id):
        # Worker A completes in its own session *between* B's two reads.
        other = session_factory()()
        try:
            other.execute(update(SendAttempt).where(SendAttempt.id == att_id).values(status="SIMULATED"))
            other.execute(update(CustomerMessage).where(CustomerMessage.id == row_id).values(delivery_status="SIMULATED"))
            other.commit()
        finally:
            other.close()
        return real(session, k, ref_id)

    monkeypatch.setattr(send_gate, "in_flight_attempt", a_finishes_first)
    enqueue(db, "customer.send", "toctou-customer", {"message_id": row_id})
    db.commit()
    run_pending()  # worker B
    db.expire_all()
    assert db.get(CustomerMessage, row_id).delivery_status == "SIMULATED"

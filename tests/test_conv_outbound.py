"""Outbound delivery on a provider-backed channel (SIMULATED sandbox adapter with failure injection), ported from
Phase 3. Meta/Twilio adapter and webhook tests were not ported: those live adapters are not part of the unified app."""

from sqlalchemy import func, select, update

from dealerflow.db import utcnow
from dealerflow.models import Conversation, IntegrationConnection, Job, Message, Notification, SandboxRecord
from dealerflow.services.conversations.outbound import queue_outbound
from tests.conv_helpers import chat, run_worker


def _queue(db, world, visitor):
    _, cid, _ = chat(world["a"]["d"], visitor, "Is the Civic available?")
    run_worker()
    conv = db.get(Conversation, cid)
    conv.channel = "TWILIO_SMS"  # use a provider-backed channel (sandbox adapter)
    conv.external_conversation_id = "+18325550000"
    conn = IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="TWILIO_SMS",
                                 external_account_id="+12815550100", status="sandbox")
    db.add(conn)
    db.commit()
    return conv, conn


def test_retryable_failure_retries_then_marks_failed_and_notifies(db, world):
    conv, conn = _queue(db, world, "f-1")
    conn.config = {"sandbox_failure_mode": "retryable"}
    db.commit()
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    for _ in range(6):
        db.execute(update(Job).where(Job.status == "pending").values(run_after=utcnow()))  # wait out the backoff
        db.commit()
        run_worker()
    db.expire_all()
    m = db.get(Message, msg.id)
    job = db.scalar(select(Job).where(Job.kind == "outbound.send", Job.key == f"outbound:{msg.id}"))
    assert job.status == "failed" and job.attempts == job.max_attempts
    assert m.delivery_status == "FAILED" and "503" in (m.delivery_error or "")
    assert db.scalar(select(func.count(Notification.id)).where(Notification.subject.contains("failed to send"))) >= 1


def test_permanent_failure_fails_fast(db, world):
    conv, conn = _queue(db, world, "f-2")
    conn.config = {"sandbox_failure_mode": "permanent"}
    db.commit()
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, msg.id).delivery_status == "FAILED"
    assert db.scalar(select(Job).where(Job.key == f"outbound:{msg.id}")).attempts == 1


def test_sandbox_send_marked_simulated(db, world):
    conv, _ = _queue(db, world, "f-3")
    msg = queue_outbound(db, conv, "Hello", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    m = db.get(Message, msg.id)
    assert m.delivery_status == "SIMULATED" and "simulated" in m.delivery_error.lower()
    rec = db.scalar(select(SandboxRecord).where(SandboxRecord.kind == "CHANNEL_OUT"))
    assert rec.payload["text"] == "Hello" and rec.payload["recipient"] == "+18325550000"  # recorded, never sent


def test_duplicate_send_job_does_not_send_twice(db, world):
    conv, _ = _queue(db, world, "f-4")
    msg = queue_outbound(db, conv, "Hello once", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    from dealerflow.services.conversations.outbound import send_job

    run_worker()
    db.expire_all()
    assert send_job(db, {"message_id": msg.id}).get("skipped") is not None  # a stray re-run is a no-op
    db.commit()
    assert db.scalar(select(func.count(SandboxRecord.id)).where(SandboxRecord.kind == "CHANNEL_OUT")) == 1

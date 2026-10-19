"""Ported from Phase 3: Modes A/B/C, authorization + evaluation gating, pauses, takeover, rate limits, business hours.

Auto-sending additionally needs Phase 4's ``inquiry_auto_response`` gate (``enable_mode_b`` turns it on)."""

from datetime import timedelta

from sqlalchemy import select

from dealerflow.db import utcnow
from dealerflow.models import (
    EvaluationRun,
    IntegrationConnection,
    Message,
    ResponseAutomationSettings,
    ResponseDraft,
)
from dealerflow.services.conversations import runtime as controls
from tests.conv_helpers import chat, enable_mode_b, run_worker


def last_draft(db, conv_id):
    return db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv_id).order_by(ResponseDraft.id.desc()))


def outbound(db, conv_id):
    return list(db.scalars(select(Message).where(Message.conversation_id == conv_id, Message.direction == "OUTBOUND")))


def test_mode_a_never_auto_sends(db, world):
    _, cid, _ = chat(world["a"]["d"], "m-a", "How much is the Civic?")
    run_worker()
    assert last_draft(db, cid).decision == "DRAFT_ONLY" and outbound(db, cid) == []


def test_mode_b_requires_authorization_and_eval(db, world):
    s = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == world["a"]["d"].id))
    s.mode = "B_FAQ_AUTOMATION"  # configured without authorization
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "m-b0", "How much is the Civic?")
    run_worker()
    d = last_draft(db, cid)
    assert d.decision == "DRAFT_ONLY" and any("authorization" in r for r in d.decision_reasons)


def test_mode_b_auto_answers_low_risk_with_templates(db, world):
    enable_mode_b(db, world)
    _, cid, _ = chat(world["a"]["d"], "m-b1", "How much is the Civic?")
    run_worker()
    d = last_draft(db, cid)
    assert d.decision == "AUTO_SEND", d.decision_reasons
    msgs = outbound(db, cid)
    assert len(msgs) == 1 and msgs[0].sender_type == "AI_AUTO" and msgs[0].delivery_status == "SIMULATED"
    assert "$18,995" in msgs[0].body


def test_mode_b_escalates_risky_questions(db, world):
    enable_mode_b(db, world)
    _, cid, _ = chat(world["a"]["d"], "m-b2", "How much is the Civic and can you do financing?")
    run_worker()
    d = last_draft(db, cid)
    assert d.decision == "DRAFT_ONLY" and "financing" in d.escalation_reasons and outbound(db, cid) == []


def test_global_dealer_channel_and_conversation_pauses(db, world):
    enable_mode_b(db, world)
    controls.set_flag(db, controls.GLOBAL_AUTOMATION_PAUSE, True, user_id=None, reason="test")
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "p-1", "How much is the Civic?")
    run_worker()
    assert any("global" in r for r in last_draft(db, cid).decision_reasons) and outbound(db, cid) == []
    controls.set_flag(db, controls.GLOBAL_AUTOMATION_PAUSE, False, user_id=None, reason="test")
    s = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == world["a"]["d"].id))
    s.paused = True
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "p-2", "How much is the Civic?")
    run_worker()
    assert any("dealership automation is paused" in r for r in last_draft(db, cid).decision_reasons)
    s.paused = False
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == world["a"]["d"].id,
                                                         IntegrationConnection.provider == "WEBSITE_CHAT"))
    conn.paused = True
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "p-3", "How much is the Civic?")
    run_worker()
    assert any("channel" in r for r in last_draft(db, cid).decision_reasons)
    conn.paused = False
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "p-4", "Is the Civic available?")
    run_worker()
    from dealerflow.models import Conversation

    conv = db.get(Conversation, cid)
    conv.human_takeover = True
    db.commit()
    chat(world["a"]["d"], "p-4", "And how much is it?")
    run_worker()
    assert any("taken over" in r for r in last_draft(db, cid).decision_reasons)


def test_emergency_pause_blocks_already_queued_auto_message(db, world):
    enable_mode_b(db, world)
    _, cid, _ = chat(world["a"]["d"], "p-5", "How much is the Civic?")
    from dealerflow.services.conversations.processing import process_message

    msg = db.scalar(select(Message).where(Message.conversation_id == cid))
    process_message(db, msg.id)  # auto-send queued, not yet delivered
    controls.set_flag(db, controls.GLOBAL_AUTOMATION_PAUSE, True, user_id=None, reason="emergency")
    db.commit()
    run_worker()
    msgs = outbound(db, cid)
    assert len(msgs) == 1 and msgs[0].delivery_status == "BLOCKED"


def test_rate_limits(db, world):
    enable_mode_b(db, world)
    s = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == world["a"]["d"].id))
    s.max_auto_replies_per_conversation_per_hour = 1
    s.min_seconds_between_auto_replies = 5
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "r-1", "How much is the Civic?")
    run_worker()
    chat(world["a"]["d"], "r-1", "Is it still available?")
    run_worker()
    d = last_draft(db, cid)
    assert d.decision == "DRAFT_ONLY" and any("rate limit" in r for r in d.decision_reasons)
    assert len([m for m in outbound(db, cid) if m.sender_type == "AI_AUTO"]) == 1


def test_business_hours_rule(db, world):
    enable_mode_b(db, world)
    s = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == world["a"]["d"].id))
    s.auto_reply_outside_business_hours = False
    from dealerflow.models import Client
    from dealerflow.services.conversations.seeding import set_weekly_hours

    set_weekly_hours(db.get(Client, world["a"]["d"].id), [None] * 7)  # closed every day → always outside hours
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "h-1", "How much is the Civic?")
    run_worker()
    assert any("business hours" in r for r in last_draft(db, cid).decision_reasons)


def test_stale_eval_or_old_engine_disables_mode(db, world):
    enable_mode_b(db, world)
    run = db.scalar(select(EvaluationRun))
    run.created_at = utcnow() - timedelta(days=45)
    db.commit()
    _, cid, _ = chat(world["a"]["d"], "e-1", "How much is the Civic?")
    run_worker()
    assert last_draft(db, cid).decision == "DRAFT_ONLY"

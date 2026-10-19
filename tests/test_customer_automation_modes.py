"""Modes A/B/C, authorization + evaluation gating, pauses, takeover, rate limits, business hours.

Ported from app/tests/test_automation_modes.py. In dealerflow, Mode B/C additionally needs Phase 4's onboarding
switch (AutomationSetting feature="inquiry_auto_response"); ``enable_mode_b`` creates it, and
``test_mode_b_stays_draft_only_without_onboarding_switch`` checks the gate. Synthetic data only.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dealerflow.db import utcnow
from dealerflow.models_customer import (
    MODE_FAQ,
    ApprovedResponseTemplate,
    CustomerConversation,
    CustomerMessage,
    EvaluationRun,
    ModeAuthorization,
    ResponseAutomationSettings,
    ResponseDraft,
)
from dealerflow.models_dealer import Client
from dealerflow.models_ops import AutomationSetting, IntegrationConnection
from dealerflow.services.customer import controls
from dealerflow.services.responder.engine import ENGINE_VERSION
from tests.customer_helpers import chat, run_worker, world  # noqa: F401


def _settings(db, client_id):
    return db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == client_id))


def enable_mode_b(db, world, key="a", onboarding_switch=True):
    cid = world[key]["id"]
    run = EvaluationRun(client_id=cid, engine_version=ENGINE_VERSION, total_cases=1, metrics={}, passed=True)
    db.add(run)
    db.flush()
    db.add(ModeAuthorization(client_id=cid, mode=MODE_FAQ, statement="test", evaluation_run_id=run.id,
                             authorized_by_id=world[key]["admin"]))
    _settings(db, cid).mode = MODE_FAQ
    for intent, body in (("price", "The listed price for the {vehicle} is {price}."),
                         ("availability", "The {vehicle} is currently available."),
                         ("greeting", "Hi{name}! Thanks for contacting {dealer_name}.")):
        db.add(ApprovedResponseTemplate(client_id=cid, intent=intent, language="en", body=body, auto_send_allowed=True,
                                        approved_by_id=world[key]["admin"], approved_at=utcnow()))
    if onboarding_switch:
        db.add(AutomationSetting(client_id=cid, feature="inquiry_auto_response", enabled=True))
    db.commit()


def last_draft(db, conv_id):
    db.expire_all()
    return db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv_id).order_by(ResponseDraft.id.desc()))


def outbound(db, conv_id):
    db.expire_all()
    return list(db.scalars(select(CustomerMessage).where(CustomerMessage.conversation_id == conv_id,
                                                         CustomerMessage.direction == "OUTBOUND")))


def test_mode_a_never_auto_sends(db, world):
    _, cid, _ = chat(world["a"]["id"], "m-a", "How much is the Civic?")
    run_worker()
    assert last_draft(db, cid).decision == "DRAFT_ONLY" and outbound(db, cid) == []


def test_mode_b_requires_authorization_and_eval(db, world):
    s = _settings(db, world["a"]["id"])
    s.mode = MODE_FAQ  # configured without authorization
    db.add(AutomationSetting(client_id=world["a"]["id"], feature="inquiry_auto_response", enabled=True))
    db.commit()
    _, cid, _ = chat(world["a"]["id"], "m-b0", "How much is the Civic?")
    run_worker()
    d = last_draft(db, cid)
    assert d.decision == "DRAFT_ONLY" and any("authorization" in r for r in d.decision_reasons)


def test_mode_b_stays_draft_only_without_onboarding_switch(db, world):
    """New in dealerflow: a valid authorization + passing evaluation is not enough without the onboarding switch."""
    enable_mode_b(db, world, onboarding_switch=False)
    _, cid, _ = chat(world["a"]["id"], "m-b-onb", "How much is the Civic?")
    run_worker()
    d = last_draft(db, cid)
    assert d.decision == "DRAFT_ONLY", d.decision_reasons
    assert any("inquiry_auto_response" in r for r in d.decision_reasons), d.decision_reasons
    assert outbound(db, cid) == []


def test_mode_b_auto_answers_low_risk_with_templates(db, world):
    enable_mode_b(db, world)
    _, cid, _ = chat(world["a"]["id"], "m-b1", "How much is the Civic?")
    run_worker()
    d = last_draft(db, cid)
    assert d.decision == "AUTO_SEND", d.decision_reasons
    msgs = outbound(db, cid)
    assert len(msgs) == 1 and msgs[0].sender_type == "AI_AUTO" and msgs[0].delivery_status == "SIMULATED"
    assert "$18,995" in msgs[0].body


def test_mode_b_escalates_risky_questions(db, world):
    enable_mode_b(db, world)
    _, cid, _ = chat(world["a"]["id"], "m-b2", "How much is the Civic and can you do financing?")
    run_worker()
    d = last_draft(db, cid)
    assert d.decision == "DRAFT_ONLY" and "financing" in d.escalation_reasons and outbound(db, cid) == []


def test_global_dealer_channel_and_conversation_pauses(db, world):
    a = world["a"]["id"]
    enable_mode_b(db, world)
    controls.set_flag(db, controls.GLOBAL_AUTOMATION_PAUSE, True, user_id=None, reason="test")
    db.commit()
    _, cid, _ = chat(a, "p-1", "How much is the Civic?")
    run_worker()
    assert any("global" in r for r in last_draft(db, cid).decision_reasons) and outbound(db, cid) == []
    controls.set_flag(db, controls.GLOBAL_AUTOMATION_PAUSE, False, user_id=None, reason="test")
    s = _settings(db, a)
    s.paused = True
    db.commit()
    _, cid, _ = chat(a, "p-2", "How much is the Civic?")
    run_worker()
    assert any("dealership automation is paused" in r for r in last_draft(db, cid).decision_reasons)
    s = _settings(db, a)
    s.paused = False
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == a,
                                                         IntegrationConnection.kind == "customer_messaging",
                                                         IntegrationConnection.provider == "website_chat"))
    conn.paused = True
    db.commit()
    _, cid, _ = chat(a, "p-3", "How much is the Civic?")
    run_worker()
    assert any("channel" in r for r in last_draft(db, cid).decision_reasons)
    conn = db.get(IntegrationConnection, conn.id)
    conn.paused = False
    db.commit()
    _, cid, _ = chat(a, "p-4", "Is the Civic available?")
    run_worker()
    db.expire_all()
    conv = db.get(CustomerConversation, cid)
    conv.human_takeover = True
    db.commit()
    chat(a, "p-4", "And how much is it?")
    run_worker()
    assert any("taken over" in r for r in last_draft(db, cid).decision_reasons)


def test_emergency_pause_blocks_already_queued_auto_message(db, world):
    enable_mode_b(db, world)
    _, cid, _ = chat(world["a"]["id"], "p-5", "How much is the Civic?")
    from dealerflow.services.customer.processing import process_message

    msg = db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == cid))
    process_message(db, msg.id)  # auto-send queued, not yet delivered
    controls.set_flag(db, controls.GLOBAL_AUTOMATION_PAUSE, True, user_id=None, reason="emergency")
    db.commit()
    run_worker()
    msgs = outbound(db, cid)
    assert len(msgs) == 1 and msgs[0].delivery_status == "BLOCKED"


def test_rate_limits(db, world):
    enable_mode_b(db, world)
    s = _settings(db, world["a"]["id"])
    s.max_auto_replies_per_conversation_per_hour = 1
    s.min_seconds_between_auto_replies = 5
    db.commit()
    _, cid, _ = chat(world["a"]["id"], "r-1", "How much is the Civic?")
    run_worker()
    chat(world["a"]["id"], "r-1", "Is it still available?")
    run_worker()
    d = last_draft(db, cid)
    assert d.decision == "DRAFT_ONLY" and any("rate limit" in r for r in d.decision_reasons)
    assert len([m for m in outbound(db, cid) if m.sender_type == "AI_AUTO"]) == 1


def test_business_hours_rule(db, world):
    enable_mode_b(db, world)
    s = _settings(db, world["a"]["id"])
    s.auto_reply_outside_business_hours = False
    c = db.get(Client, world["a"]["id"])
    c.business_hours = {str(wd): None for wd in range(7)}  # closed every day → always outside hours
    db.commit()
    _, cid, _ = chat(world["a"]["id"], "h-1", "How much is the Civic?")
    run_worker()
    assert any("business hours" in r for r in last_draft(db, cid).decision_reasons)


def test_stale_eval_or_old_engine_disables_mode(db, world):
    enable_mode_b(db, world)
    run = db.scalar(select(EvaluationRun).where(EvaluationRun.client_id == world["a"]["id"]))
    run.created_at = utcnow() - timedelta(days=45)
    db.commit()
    _, cid, _ = chat(world["a"]["id"], "e-1", "How much is the Civic?")
    run_worker()
    assert last_draft(db, cid).decision == "DRAFT_ONLY"

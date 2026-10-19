"""Phase 3 outbound: staff approval, simulated vs live delivery, emergency stop, duplicate/crash safety, auto-send gates."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from dealerflow.db import session_scope, utcnow
from dealerflow.jobs import STALE_LOCK
from dealerflow.models import (
    AgentState,
    AuditLog,
    AutomationSetting,
    ConversationMessage,
    IntegrationConnection,
    Job,
    ResponseDraft,
    ResponseTemplate,
)
from dealerflow.models_conversations import MODE_FAQ
from tests.conv_helpers import chat, only_conversation, outbound, pending_draft, setup_dealer
from tests.dealer_helpers import drain


def _ready(client, d, text="Is the Accord available?"):
    chat(client, d["key"], text)
    drain()
    conv = only_conversation(d["id"])
    return conv, pending_draft(conv.id)


def test_staff_review_edit_approve_is_simulated_and_never_reaches_the_visitor(client):
    d = setup_dealer()
    conv, draft = _ready(client, d)
    edited = draft.body + " Thanks, Sam."
    r = client.post(f"/api/v1/dealers/{d['id']}/conversations/{conv.id}/reply", headers=d["h"]["reviewer"],
                    json={"body": edited, "draft_id": draft.id})
    assert r.json()["status"] == "queued"
    drain()
    [msg] = outbound(conv.id)
    assert msg.delivery_status == "simulated" and msg.body == edited and msg.sender_type == "staff"
    assert "DF_CUSTOMER_MESSAGING_LIVE_ENABLED" in msg.delivery_error
    poll = client.get(f"/api/public/chat/{d['key']}/messages", params={"visitor_id": "visitor-0001"}).json()
    assert poll["messages"] == []  # SIMULATED replies stay inside the platform
    with session_scope() as db:
        assert db.get(ResponseDraft, draft.id).status == "sent"
        actions = {a.action for a in db.scalars(select(AuditLog).where(AuditLog.action.like("conversation.%")))}
        assert {"conversation.draft_edited", "conversation.reply_approved", "conversation.message_queued",
                "conversation.message_simulated"} <= actions
        c = only_conversation(d["id"])
        assert c.first_response_at is None  # a simulated reply is not a real response


def test_live_website_chat_needs_env_switch_and_connected_channel(client, monkeypatch):
    from dealerflow.config import get_settings

    d = setup_dealer()
    conv, draft = _ready(client, d)
    monkeypatch.setenv("DF_CUSTOMER_MESSAGING_LIVE_ENABLED", "true")
    get_settings.cache_clear()
    with session_scope() as db:  # switch on but no authorized connection → still simulated
        from dealerflow.services.conversations.outbound import live_delivery
        from dealerflow.models import Conversation

        assert live_delivery(db, db.get(Conversation, conv.id))[0] is False
        db.add(IntegrationConnection(client_id=d["id"], kind="customer_messaging", provider="website_chat", status="connected",
                                     authorization_note="Dealer approved website chat replies (test)"))
    client.post(f"/api/v1/dealers/{d['id']}/conversations/{conv.id}/reply", headers=d["h"]["admin"],
                json={"body": draft.body, "draft_id": draft.id})
    drain()
    assert outbound(conv.id)[0].delivery_status == "sent"
    poll = client.get(f"/api/public/chat/{d['key']}/messages", params={"visitor_id": "visitor-0001"}).json()
    assert [m["text"] for m in poll["messages"]] == [draft.body]
    assert only_conversation(d["id"]).first_response_at is not None


def test_emergency_stop_blocks_queued_replies(client):
    d = setup_dealer()
    conv, draft = _ready(client, d)
    client.post(f"/api/v1/dealers/{d['id']}/conversations/{conv.id}/reply", headers=d["h"]["admin"],
                json={"body": draft.body, "draft_id": draft.id})
    with session_scope() as db:
        st = db.get(AgentState, 1) or AgentState(id=1)
        st.status = "emergency_stopped"
        db.add(st)
    drain()
    [msg] = outbound(conv.id)
    assert msg.delivery_status == "blocked" and "emergency stop" in msg.delivery_error
    with session_scope() as db:  # the draft goes back to staff so it can be re-sent deliberately later
        assert db.get(ResponseDraft, draft.id).status == "pending_review"


def test_double_submit_and_duplicate_jobs_send_once(client):
    d = setup_dealer()
    conv, draft = _ready(client, d)
    for _ in range(2):
        client.post(f"/api/v1/dealers/{d['id']}/conversations/{conv.id}/reply", headers=d["h"]["admin"],
                    json={"body": "We have it in stock — when would you like to visit?"})
    from dealerflow.services.conversations.outbound import send_job

    drain()
    msgs = outbound(conv.id)
    assert len(msgs) == 1 and msgs[0].delivery_status == "simulated"
    with session_scope() as db:  # running the job again is a no-op
        assert send_job(db, {"message_id": msgs[0].id, "client_id": d["id"]}) == {"skipped": "simulated"}


def test_crashed_send_becomes_uncertain_and_is_never_resent_automatically(client):
    from dealerflow.jobs import DeferJob
    from dealerflow.services.conversations.outbound import send_job

    d = setup_dealer()
    conv, draft = _ready(client, d)
    client.post(f"/api/v1/dealers/{d['id']}/conversations/{conv.id}/reply", headers=d["h"]["admin"],
                json={"body": draft.body, "draft_id": draft.id})
    msg_id = None
    with session_scope() as db:  # simulate a worker that claimed the message and died
        m = db.scalar(select(ConversationMessage).where(ConversationMessage.direction == "outbound"))
        msg_id = m.id
        m.delivery_status, m.sending_started_at = "sending", utcnow()
    with session_scope() as db, pytest.raises(DeferJob):  # owner may still be alive: wait, never overwrite
        send_job(db, {"message_id": msg_id, "client_id": d["id"]})
    with session_scope() as db:
        db.execute(update(ConversationMessage).where(ConversationMessage.id == msg_id)
                   .values(sending_started_at=utcnow() - STALE_LOCK - timedelta(minutes=1)))
    with session_scope() as db:
        assert send_job(db, {"message_id": msg_id, "client_id": d["id"]}) == {"uncertain": True}
    assert outbound(conv.id)[0].delivery_status == "uncertain"
    # Staff reconcile: "not delivered" re-queues it under a new job key; delivery then happens once.
    r = client.post(f"/dealers/{d['id']}/conversations/{conv.id}/messages/{msg_id}/reconcile", headers=d["h"]["admin"],
                    data={"delivered": "0"}, follow_redirects=False)
    assert r.status_code == 303
    drain()
    assert outbound(conv.id)[0].delivery_status == "simulated"
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "conversation_send")) == 2


def test_staff_text_with_unsupported_claims_needs_acknowledgement(client):
    d = setup_dealer()
    conv, draft = _ready(client, d)
    risky = "Great news, it has no accidents and you're pre-approved!"
    r = client.post(f"/api/v1/dealers/{d['id']}/conversations/{conv.id}/reply", headers=d["h"]["admin"], json={"body": risky})
    assert r.json()["status"] == "needs_acknowledgement" and r.json()["warnings"]
    assert outbound(conv.id) == []
    r = client.post(f"/api/v1/dealers/{d['id']}/conversations/{conv.id}/reply", headers=d["h"]["admin"],
                    json={"body": risky, "acknowledge_warnings": True})
    assert r.json()["status"] == "queued"
    with session_scope() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "conversation.staff_override"))


def _enable_auto_send(client, d):
    """Every gate for automatic replies: passing evaluation + written authorization (mode B), onboarding gate,
    an approved auto-send template, and the live channel. Returns the evaluation run id."""
    from dealerflow.db import utcnow as now

    r = client.post(f"/dealers/{d['id']}/responses/evaluate", headers=d["h"]["admin"], follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        from dealerflow.models import ResponseEvaluationRun

        run = db.scalar(select(ResponseEvaluationRun).where(ResponseEvaluationRun.client_id == d["id"]))
        assert run.passed
        db.add(ResponseTemplate(client_id=d["id"], intent="availability", language="en", auto_send_allowed=True,
                                body="Yes! The {vehicle} is currently showing as available in our inventory.", approved_at=now()))
        db.add(AutomationSetting(client_id=d["id"], feature="inquiry_auto_response", enabled=True))
        run_id = run.id
    r = client.post(f"/dealers/{d['id']}/responses/mode", headers=d["h"]["admin"], follow_redirects=False,
                    data={"mode": MODE_FAQ, "evaluation_run_id": str(run_id),
                          "statement": "Dealer principal J. Doe approved FAQ automation by e-mail on 2026-10-19 (test)"})
    assert r.status_code == 303
    return run_id


def test_auto_send_requires_every_gate(client):
    d = setup_dealer()
    # Without authorization nothing is automatic.
    conv, draft = _ready(client, d, "Is the Accord available?")
    assert draft.decision == "draft_only"
    _enable_auto_send(client, d)
    chat(client, d["key"], "Is the Camry available?", visitor="visitor-0002")
    drain()
    with session_scope() as db:
        auto = list(db.scalars(select(ConversationMessage).where(ConversationMessage.sender_type == "ai_auto")))
        assert len(auto) == 1 and auto[0].delivery_status == "simulated"  # automatic, but still SIMULATED delivery
        d2 = db.scalar(select(ResponseDraft).where(ResponseDraft.sent_message_id == auto[0].id))
        assert d2.status == "auto_sent" and d2.decision == "auto_send"
    # Escalation topics are never automatic, even with every gate open.
    chat(client, d["key"], "Can you get me financing for the Camry?", visitor="visitor-0003")
    drain()
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(ConversationMessage).where(ConversationMessage.sender_type == "ai_auto")) == 1
    # Pausing the dealership stops automatic replies.
    client.post(f"/dealers/{d['id']}/responses/pause", headers=d["h"]["admin"], data={"paused": "1", "reason": "test"})
    chat(client, d["key"], "Is the F-150 available?", visitor="visitor-0004")
    drain()
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(ConversationMessage).where(ConversationMessage.sender_type == "ai_auto")) == 1


def test_auto_send_revalidates_facts_at_send_time(client):
    from dealerflow.models_dealer import Vehicle

    d = setup_dealer()
    _enable_auto_send(client, d)
    chat(client, d["key"], "Is the Camry available?", visitor="visitor-0005")
    from dealerflow.jobs import run_pending

    with session_scope() as db:  # process the inquiry only, then make the vehicle stale before the send job runs
        db.execute(update(Job).where(Job.kind != "conversation_process").values(run_after=utcnow() + timedelta(days=1)))
    run_pending()
    with session_scope() as db:
        v = db.scalar(select(Vehicle).where(Vehicle.client_id == d["id"], Vehicle.stock_number == "B200"))
        v.stale = True
        db.execute(update(Job).values(run_after=utcnow()))
    drain()
    with session_scope() as db:
        m = db.scalar(select(ConversationMessage).where(ConversationMessage.sender_type == "ai_auto"))
        assert m.delivery_status == "blocked" and "changed or became unverified" in m.delivery_error


def test_ai_rephrasing_off_or_failing_falls_back_to_the_grounded_draft(client, monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.integrations import llm

    d = setup_dealer()
    monkeypatch.setenv("DF_AI_ENABLED", "true")  # enabled but no API key → unavailable
    get_settings.cache_clear()
    assert llm.ai_available() is False
    monkeypatch.setenv("DF_ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    get_settings.cache_clear()

    def boom(*a, **k):
        raise llm.AIUnavailable("AI provider error 401")  # what an invalid key produces

    monkeypatch.setattr(llm, "complete", boom)
    conv, draft = _ready(client, d)
    assert draft.generated_by == "template" and "available" in draft.body and "$5,995" not in draft.body
    with session_scope() as db:
        assert "sk-test" not in " ".join(str(a.data) for a in db.scalars(select(AuditLog)))


def test_ai_rephrasing_output_that_adds_facts_is_discarded(client, monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.integrations import llm

    d = setup_dealer()
    monkeypatch.setenv("DF_AI_ENABLED", "true")
    monkeypatch.setenv("DF_ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    get_settings.cache_clear()

    def invent(db, **kw):
        return llm.AIResult(text="Yes! It's accident-free, comes with a warranty, and it's $4,000 off today.", model="test",
                            input_tokens=10, output_tokens=10, estimated_cost=0.0)

    monkeypatch.setattr(llm, "complete", invent)
    conv, draft = _ready(client, d)
    assert draft.generated_by == "template" and "warranty" not in draft.body and "$4,000" not in draft.body

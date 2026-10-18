"""Ingestion, dedupe, CRM/vehicle linking, escalation, human handoff, opt-out, tenant isolation."""

import pytest
from sqlalchemy import func, select

from app.models import (
    ContactSuppression,
    Conversation,
    CustomerLead,
    Dealership,
    Escalation,
    Job,
    Message,
    Notification,
    ResponseDraft,
)
from app.services.inbox.outbound import queue_outbound
from app.services.inbox.processing import ingest_inbound
from tests.conftest import chat, run_worker


def test_duplicate_events_are_deduplicated(db, world):
    m1, c1, created1 = chat(world["a"]["d"], "visitor-1", "Is the Civic available?", msg_id="dup-1")
    m2, c2, created2 = chat(world["a"]["d"], "visitor-1", "Is the Civic available?", msg_id="dup-1")
    assert created1 and not created2 and m1 == m2 and c1 == c2
    assert db.scalar(select(func.count(Message.id))) == 1
    assert db.scalar(select(func.count(Job.id)).where(Job.type == "inbox.process_message")) == 1
    run_worker()
    assert db.scalar(select(func.count(ResponseDraft.id))) == 1


def test_links_vehicle_lead_and_creates_draft(db, world):
    _, conv_id, _ = chat(world["a"]["d"], "visitor-2", "Hi, how much is the 2019 Civic?")
    run_worker()
    conv = db.get(Conversation, conv_id)
    assert conv.vehicle_id == world["a"]["civic"].id
    lead = db.get(CustomerLead, conv.lead_id)
    assert lead.vehicle_id == world["a"]["civic"].id and lead.dealership_id == world["a"]["d"].id
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv_id))
    assert draft.status == "PENDING_REVIEW" and "$18,995" in draft.body  # Mode A: draft only
    assert conv.summary["vehicle_of_interest"].startswith("2019 Honda Civic")
    assert conv.summary["recommended_next_step"]
    assert db.scalar(select(func.count(Message.id)).where(Message.direction == "OUTBOUND")) == 0


def test_escalation_notifies_assigned_employee_with_summary(db, world):
    _, conv_id, _ = chat(world["a"]["d"], "visitor-3", "Is the Civic available?")
    run_worker()
    conv = db.get(Conversation, conv_id)
    conv.assigned_user_id = world["a"]["staff"].id
    db.commit()
    chat(world["a"]["d"], "visitor-3", "Do you offer financing? My credit is bad.")
    run_worker()
    esc = db.scalar(select(Escalation).where(Escalation.conversation_id == conv_id))
    assert {"financing", "credit"} <= set(esc.reasons)
    assert "Next step" in esc.summary and esc.recommended_action
    n = db.scalar(select(Notification).where(Notification.user_id == world["a"]["staff"].id,
                                             Notification.kind == "ESCALATION", Notification.channel == "IN_APP"))
    assert n is not None and "financing" in n.subject and "Recent messages" in n.body
    # Nobody from the other dealership is notified.
    assert db.scalar(select(func.count(Notification.id)).where(Notification.user_id.in_(
        [world["b"]["admin"].id, world["b"]["staff"].id]))) == 0


def test_human_request_stops_automation(db, world):
    _, conv_id, _ = chat(world["a"]["d"], "visitor-4", "Can I talk to a real person please?")
    run_worker()
    conv = db.get(Conversation, conv_id)
    assert conv.human_requested
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv_id))
    assert "human_request" in draft.escalation_reasons and draft.decision == "DRAFT_ONLY"


def test_opt_out_stops_everything(db, world):
    _, conv_id, _ = chat(world["a"]["d"], "visitor-5", "Is the Civic available?")
    run_worker()
    conv = db.get(Conversation, conv_id)
    queued = queue_outbound(db, conv, "Automated follow-up", sender_type="AI_AUTO")
    db.commit()
    chat(world["a"]["d"], "visitor-5", "STOP")
    # Process the opt-out before the queued send runs.
    from app.services.inbox.processing import process_message

    last = db.scalar(select(Message).where(Message.conversation_id == conv_id, Message.direction == "INBOUND")
                     .order_by(Message.id.desc()))
    process_message(db, last.id)
    db.commit()
    run_worker()
    db.expire_all()
    conv = db.get(Conversation, conv_id)
    assert conv.opted_out
    assert db.get(Message, queued.id).delivery_status == "BLOCKED"
    assert db.scalar(select(ContactSuppression).where(ContactSuppression.address == "web:visitor-5")) is not None
    assert db.scalar(select(ResponseDraft).where(ResponseDraft.in_reply_to_message_id == last.id)) is None
    # Even a staff reply is blocked from the platform afterwards.
    staff_msg = queue_outbound(db, conv, "Hello?", sender_type="STAFF", user_id=world["a"]["staff"].id)
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(Message, staff_msg.id).delivery_status == "BLOCKED"


def test_cross_tenant_vehicle_reference_is_ignored(db, world):
    d_a = db.get(Dealership, world["a"]["d"].id)
    res = ingest_inbound(db, dealership=d_a, channel="WEBSITE_CHAT", external_conversation_id="web:x", text="hi",
                         provider_message_id="x-1", vehicle_id=world["b"]["civic"].id)
    assert res.conversation.vehicle_id is None


def test_provider_id_collision_across_tenants_rejected(db, world):
    d_a, d_b = db.get(Dealership, world["a"]["d"].id), db.get(Dealership, world["b"]["d"].id)
    ingest_inbound(db, dealership=d_a, channel="META_MESSENGER", external_conversation_id="psid-1", text="hi",
                   provider_message_id="mid.123")
    with pytest.raises(ValueError):
        ingest_inbound(db, dealership=d_b, channel="META_MESSENGER", external_conversation_id="psid-1", text="hi",
                       provider_message_id="mid.123")


def test_sensitive_data_redacted_and_escalated(db, world):
    m, conv_id, _ = chat(world["a"]["d"], "visitor-6", "my ssn is 123-45-6789 for the credit app")
    run_worker()
    assert "123-45-6789" not in db.get(Message, m).body
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv_id))
    assert "sensitive_data" in draft.escalation_reasons

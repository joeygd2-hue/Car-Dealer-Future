"""Phase 3 inbound pipeline: lead capture, dedupe, grounded drafts, escalation, test-drive requests, opt-out."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from dealerflow.db import session_scope, utcnow
from dealerflow.models import (
    Appointment,
    AuditLog,
    Conversation,
    ConversationEscalation,
    ConversationMessage,
    CustomerContact,
    CustomerLead,
    CustomerSuppression,
    ResponseDraft,
    StaffNotification,
)
from dealerflow.models_dealer import Vehicle
from tests.conv_helpers import chat, only_conversation, pending_draft, setup_dealer
from tests.dealer_helpers import drain


def test_website_inquiry_creates_one_lead_and_a_grounded_draft(client):
    d = setup_dealer()
    r = chat(client, d["key"], "Hi, is the 2003 Honda Accord still available and what's the price?", msg_id="m1",
             name="Dana Buyer", email="Dana@Buyer.example")
    assert r.status_code == 200 and r.json()["duplicate"] is False
    # The same message delivered again (widget retry) is stored once.
    assert chat(client, d["key"], "Hi, is the 2003 Honda Accord still available and what's the price?", msg_id="m1").json()["duplicate"]
    drain()
    conv = only_conversation(d["id"])
    with session_scope() as db:
        accord = db.scalar(select(Vehicle).where(Vehicle.client_id == d["id"], Vehicle.stock_number == "A100"))
        assert conv.vehicle_id == accord.id and conv.channel == "website_chat" and conv.status == "awaiting_staff"
        contact = db.get(CustomerContact, conv.contact_id)
        assert contact.display_name == "Dana Buyer" and contact.email == "dana@buyer.example"
        lead = db.get(CustomerLead, conv.lead_id)
        assert lead.contact_id == contact.id and lead.vehicle_id == accord.id and lead.source == "website_chat"
        assert db.scalar(select(func.count()).select_from(ConversationMessage).where(ConversationMessage.client_id == d["id"])) == 1
    draft = pending_draft(conv.id)
    assert "$5,995" in draft.body and "available" in draft.body  # verified inventory values only
    assert set(draft.facts_used) >= {"availability", "price"} and draft.decision == "draft_only"
    assert any("draft-only" in r for r in draft.decision_reasons)
    with session_scope() as db:  # nothing was sent, and staff were told a draft is waiting
        assert db.scalar(select(func.count()).select_from(ConversationMessage).where(ConversationMessage.direction == "outbound")) == 0
        assert db.scalar(select(StaffNotification).where(StaffNotification.client_id == d["id"], StaffNotification.kind == "new_lead"))


def test_returning_customer_joins_the_same_contact(client):
    d = setup_dealer()
    chat(client, d["key"], "Do you have the Camry?", visitor="visitor-aaaa", msg_id="1", email="pat@buyer.example")
    chat(client, d["key"], "And what are your hours?", visitor="visitor-aaaa", msg_id="2")
    chat(client, d["key"], "Hello again, about the F-150", visitor="visitor-bbbb", msg_id="3", email="PAT@buyer.example")
    drain()
    with session_scope() as db:
        convs = list(db.scalars(select(Conversation).where(Conversation.client_id == d["id"])))
        assert len(convs) == 2  # one per chat visitor
        assert len({c.contact_id for c in convs}) == 1  # same e-mail → same customer record
        assert db.scalar(select(func.count()).select_from(CustomerContact).where(CustomerContact.client_id == d["id"])) == 1


def test_stale_or_unverified_inventory_is_never_quoted(client):
    d = setup_dealer()
    with session_scope() as db:
        v = db.scalar(select(Vehicle).where(Vehicle.client_id == d["id"], Vehicle.stock_number == "A100"))
        v.last_seen_at = utcnow() - timedelta(hours=72)
    chat(client, d["key"], "What's the price of the Accord and is it available?")
    drain()
    draft = pending_draft(only_conversation(d["id"]).id)
    assert "$5,995" not in draft.body and "uncertain_inventory" in draft.escalation_reasons
    assert "confirm" in draft.body.lower()
    with session_scope() as db:
        esc = db.scalar(select(ConversationEscalation).where(ConversationEscalation.client_id == d["id"]))
        assert esc.status == "open" and "Verify current price" in esc.recommended_action


def test_spanish_inquiry_gets_a_spanish_draft_and_financing_is_escalated(client):
    d = setup_dealer()
    chat(client, d["key"], "Hola, ¿el Camry sigue disponible? ¿Ofrecen financiamiento?")
    drain()
    conv = only_conversation(d["id"])
    draft = pending_draft(conv.id)
    assert conv.preferred_language == "es" and draft.language == "es"
    assert "financing" in draft.escalation_reasons and "financiamiento" in draft.body
    assert "aprobad" not in draft.body.lower()  # never promises approval
    assert conv.status == "escalated" or only_conversation(d["id"]).status == "escalated"


def test_business_details_are_used_only_after_approval(client):
    d = setup_dealer(approved=False)
    chat(client, d["key"], "What are your hours and where are you located?")
    drain()
    draft = pending_draft(only_conversation(d["id"]).id)
    assert "123 Main St" not in draft.body and "missing_data" in draft.escalation_reasons


def test_test_drive_request_is_recorded_not_promised(client):
    d = setup_dealer()
    chat(client, d["key"], "Can I test drive the Accord tomorrow at 3pm? My number is 281-555-0142", name="Lee")
    drain()
    conv = only_conversation(d["id"])
    draft = pending_draft(conv.id)
    with session_scope() as db:
        appt = db.scalar(select(Appointment).where(Appointment.client_id == d["id"]))
        assert appt.status == "pending_confirmation" and appt.confirmed_start is None and appt.requested_start is not None
        assert appt.customer_phone.replace("-", "") == "2815550142" and appt.availability_checked == "no_calendar"
    assert "isn't confirmed" in draft.body or "not confirmed" in draft.body
    assert "appointment requests are confirmed by staff" in draft.decision_reasons
    assert conv.appointment_status == "pending_confirmation" and conv.lead_qualified


def test_opt_out_stops_everything(client):
    d = setup_dealer()
    chat(client, d["key"], "Is the F-150 available?", msg_id="1")
    drain()
    conv = only_conversation(d["id"])
    first = pending_draft(conv.id)
    chat(client, d["key"], "STOP", msg_id="2")
    drain()
    with session_scope() as db:
        c = db.get(Conversation, conv.id)
        assert c.opted_out and db.get(ResponseDraft, first.id).status == "superseded"
        assert db.scalar(select(CustomerSuppression).where(CustomerSuppression.client_id == d["id"],
                                                           CustomerSuppression.address == c.external_conversation_id))
        assert db.scalar(select(func.count()).select_from(ResponseDraft).where(ResponseDraft.status == "pending_review")) == 0
    # Staff cannot send from the platform any more.
    r = client.post(f"/api/v1/dealers/{d['id']}/conversations/{conv.id}/reply", headers=d["h"]["admin"], json={"body": "Sorry!"})
    assert r.status_code == 400 and "opted out" in r.json()["detail"]


def test_audit_trail_has_ids_but_no_customer_text(client):
    d = setup_dealer()
    chat(client, d["key"], "Is the Accord available? Email me at secret.buyer@example.com", email="secret.buyer@example.com")
    drain()
    with session_scope() as db:
        rows = list(db.scalars(select(AuditLog).where(AuditLog.action.like("conversation.%"))))
        assert {"conversation.message_received", "conversation.draft_created"} <= {r.action for r in rows}
        blob = " ".join(str(r.data) for r in rows)
        assert "secret.buyer" not in blob and "Accord available" not in blob


def test_evaluation_set_passes(db):
    from dealerflow.services.conversations.evaluation import run_cases

    out = run_cases()
    assert out["total"] == 46 and out["passed"], out["failures"][:3]
    assert out["metrics"]["escalation_recall"] == 1.0 and out["metrics"]["opt_out_accuracy"] == 1.0

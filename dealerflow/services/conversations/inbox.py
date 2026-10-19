"""Inbound pipeline (ported from Phase 3 ``services/inbox/processing.py``):

ingest (dedupe, link contact/lead/vehicle) → job ``conversation_process``: analyze → draft from verified facts →
decide (draft-only unless every automation gate passes) → queue/escalate/notify → summarize.

Duplicates: a message is stored once per (client, channel, provider message id); re-delivery returns the stored
message. A contact is matched by its channel identity, then by e-mail within the same dealership, so a repeat
inquiry joins the existing contact and conversation instead of creating new ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...db import utcnow
from ...jobs import enqueue, handler
from ...models import (
    Client,
    Conversation,
    ConversationMessage,
    CustomerContact,
    CustomerIdentity,
    CustomerLead,
    ResponseDraft,
)
from ...models_dealer import Vehicle
from . import escalation, notify
from .appointments import customer_change_requested, request_from_conversation
from .common import conversation_link, local_now, log
from .outbound import mark_opted_out, queue_outbound
from .policy import decide, settings_row
from .responder import analysis as A
from .responder.engine import ENGINE_VERSION, R_HUMAN, compose, llm_rephrase
from .responder.facts import build_fact_sheet, resolve_vehicle
from .responder.language import detect_language
from .summaries import build_summary, summary_text

JOB = "conversation_process"


@dataclass
class IngestResult:
    message: ConversationMessage
    conversation: Conversation
    created: bool


def _contact_for(db: Session, client_id: int, channel: str, sender_id: str, hints: dict, simulated: bool) -> CustomerContact:
    ident = db.scalar(select(CustomerIdentity).where(CustomerIdentity.client_id == client_id, CustomerIdentity.channel == channel,
                                                     CustomerIdentity.external_id == sender_id))
    contact = db.get(CustomerContact, ident.contact_id) if ident else None
    if contact is None:
        email = (hints.get("email") or "").strip().lower() or None
        if email:
            contact = db.scalar(select(CustomerContact).where(CustomerContact.client_id == client_id, CustomerContact.email == email)
                                .order_by(CustomerContact.id))
        if contact is None:
            contact = CustomerContact(client_id=client_id, contact_source=channel, simulated=simulated)
            db.add(contact)
            db.flush()
        db.add(CustomerIdentity(client_id=client_id, contact_id=contact.id, channel=channel, external_id=sender_id))
    if hints.get("name") and not contact.display_name:
        contact.display_name = str(hints["name"])[:200]
    if hints.get("email") and not contact.email:
        contact.email = str(hints["email"]).strip().lower()[:200]
    if hints.get("phone") and not contact.phone:
        contact.phone = str(hints["phone"])[:40]
    return contact


def ingest_inbound(db: Session, *, client: Client, channel: str, external_conversation_id: str, text: str,
                   provider_message_id: str | None, source_timestamp: datetime | None = None, contact_hints: dict | None = None,
                   vehicle_id: int | None = None, simulated: bool = False) -> IngestResult:
    if provider_message_id:
        existing = db.scalar(select(ConversationMessage).where(
            ConversationMessage.client_id == client.id, ConversationMessage.channel == channel,
            ConversationMessage.provider_message_id == provider_message_id))
        if existing:
            return IngestResult(existing, db.get(Conversation, existing.conversation_id), False)
    if vehicle_id is not None:
        v = db.get(Vehicle, vehicle_id)
        if v is None or v.client_id != client.id:
            vehicle_id = None  # ignore cross-dealership or unknown vehicle references
    contact = _contact_for(db, client.id, channel, external_conversation_id, contact_hints or {}, simulated)
    conv = db.scalar(select(Conversation).where(Conversation.client_id == client.id, Conversation.channel == channel,
                                                Conversation.external_conversation_id == external_conversation_id))
    created_conv = conv is None
    now = utcnow()
    if conv is None:
        lang, _ = detect_language(text)
        lead = CustomerLead(client_id=client.id, contact_id=contact.id, vehicle_id=vehicle_id, source=channel, simulated=simulated)
        db.add(lead)
        db.flush()
        conv = Conversation(client_id=client.id, channel=channel, external_conversation_id=external_conversation_id,
                            contact_id=contact.id, vehicle_id=vehicle_id, lead_id=lead.id, preferred_language=lang,
                            simulated=simulated, first_inbound_at=now)
        db.add(conv)
        db.flush()
    elif vehicle_id and not conv.vehicle_id:
        conv.vehicle_id = vehicle_id
    sensitive = A.detect_sensitive(text)  # detect before redaction so the signal is not lost
    msg = ConversationMessage(client_id=client.id, conversation_id=conv.id, channel=channel, direction="inbound",
                              sender_type="customer", body=A.redact(text)[:10000], provider_message_id=provider_message_id,
                              source_timestamp=source_timestamp, delivery_status="received",
                              analysis={"sensitive_detected": sensitive} if sensitive else {})
    try:
        with db.begin_nested():
            db.add(msg)
            db.flush()
    except IntegrityError:  # concurrent duplicate delivery
        existing = db.scalar(select(ConversationMessage).where(
            ConversationMessage.client_id == client.id, ConversationMessage.channel == channel,
            ConversationMessage.provider_message_id == provider_message_id))
        return IngestResult(existing, db.get(Conversation, existing.conversation_id), False)
    conv.last_inbound_at = now
    if conv.status in ("closed", "awaiting_customer", "open"):
        conv.status = "awaiting_staff"
    log(db, "conversation.message_received", client_id=client.id, entity_type="conversation_message", entity_id=msg.id,
        actor_type="customer", channel=channel, conversation_id=conv.id, new_conversation=created_conv)
    enqueue(db, JOB, f"{JOB}:{msg.id}", {"message_id": msg.id, "client_id": client.id}, max_attempts=4)
    return IngestResult(msg, conv, True)


def _messages(db: Session, conv: Conversation) -> list[ConversationMessage]:
    return list(db.scalars(select(ConversationMessage).where(
        ConversationMessage.client_id == conv.client_id, ConversationMessage.conversation_id == conv.id).order_by(ConversationMessage.id)))


def _llm_complete(db: Session, client_id: int):
    """Claude rephrasing, only when AI is enabled; otherwise None (the deterministic composer is always the source)."""
    from ...integrations import llm

    if not llm.ai_available():
        return None

    def complete(system: str, prompt: str) -> str | None:
        try:
            result = llm.complete(db, system=system, prompt=prompt, feature="conversation_rephrase", client_id=client_id,
                                  max_tokens=600)
        except llm.AIUnavailable:
            return None
        return None if result.refused else result.text
    return complete


def process_message(db: Session, message_id: int) -> dict:
    msg = db.get(ConversationMessage, message_id)
    if msg is None or msg.direction != "inbound":
        return {"skipped": "message not found"}
    if (msg.analysis or {}).get("processed"):
        return {"skipped": "already processed"}
    conv = db.get(Conversation, msg.conversation_id)
    client = db.get(Client, conv.client_id)
    settings = settings_row(db, client.id)
    lang, lang_conf = detect_language(msg.body, prior=conv.preferred_language)
    if lang_conf >= 0.6:
        conv.preferred_language = lang
    lang = conv.preferred_language
    now_local = local_now(client.timezone)
    an = A.analyze(msg.body, lang, now_local)
    for kind in (msg.analysis or {}).get("sensitive_detected", []):
        if kind not in an.sensitive_data:
            an.sensitive_data.append(kind)
    msg.language = lang
    contact = db.get(CustomerContact, conv.contact_id) if conv.contact_id else None
    if contact and (an.name or an.emails or an.phones):
        contact.display_name = contact.display_name or an.name
        contact.email = contact.email or (an.emails[0].lower() if an.emails else None)
        contact.phone = contact.phone or (an.phones[0] if an.phones else None)
        contact.contact_source = contact.contact_source or f"provided by customer via {conv.channel}"
    if contact:
        contact.preferred_language = lang

    vehicle, ambiguous = resolve_vehicle(db, client.id, an, msg.body, conv.vehicle_id)
    if vehicle and conv.vehicle_id != vehicle.id:
        conv.vehicle_id = vehicle.id
        if conv.lead_id:
            lead = db.get(CustomerLead, conv.lead_id)
            lead.vehicle_id = lead.vehicle_id or vehicle.id
    msg.analysis = {**an.to_dict(), "processed": True, "vehicle_id": vehicle.id if vehicle else None}

    if an.opt_out:
        mark_opted_out(db, conv, source=f"customer message #{msg.id}")
        notify.notify(db, client.id, kind="system", subject=f"Customer opted out (conversation #{conv.id})",
                      body="The customer asked not to be contacted. Automation is stopped for this conversation.",
                      link=conversation_link(client.id, conv.id), assigned_user_id=conv.assigned_user_id,
                      dedupe_key=f"optout:{conv.id}")
        build_summary(db, conv)
        return {"opt_out": True}
    if an.human_request:
        conv.human_requested = True

    fs = build_fact_sheet(db, client, settings, lang, vehicle, ambiguous)
    history = _messages(db, conv)
    prior_out = [m for m in history if m.direction == "outbound"]
    offered = any("test drive" in m.body.lower() or "prueba de manejo" in m.body.lower() for m in prior_out)
    plan = compose(an, fs, msg.body, contact_name=(contact.display_name.split()[0] if contact and contact.display_name else None),
                   has_contact_method=bool(contact and (contact.phone or contact.email)),
                   already_offered_test_drive=offered, is_first_reply=not prior_out, now_local=now_local)
    if R_HUMAN in plan.escalation_reasons:
        conv.human_requested = True
    if not plan.no_reply and not plan.escalation_reasons:
        text = llm_rephrase(_llm_complete(db, client.id), plan, fs)
        if text:
            plan.body, plan.generated_by = text, "template+llm"

    if A.CANCEL_APPOINTMENT in an.intents:
        customer_change_requested(db, conv, msg.body)
    if A.TEST_DRIVE in an.intents:
        conv.lead_qualified = True
    appt = None
    if plan.appointment_request is not None and plan.appointment_request_in_hours is not False:
        appt = request_from_conversation(db, conv, plan.appointment_request,
                                         name=contact.display_name if contact else None,
                                         phone=contact.phone if contact else None, email=contact.email if contact else None)

    for old in db.scalars(select(ResponseDraft).where(ResponseDraft.client_id == client.id, ResponseDraft.conversation_id == conv.id,
                                                      ResponseDraft.status == "pending_review")):
        old.status = "superseded"
    decision = decide(db, conv, plan, fs)
    draft = ResponseDraft(client_id=client.id, conversation_id=conv.id, in_reply_to_message_id=msg.id, body=plan.body,
                          language=lang, confidence=plan.confidence, intents=plan.intents, facts_used=plan.facts_used,
                          unresolved=plan.unresolved,
                          fact_snapshot={f.key: f.value for f in fs.facts if f.key in set(plan.facts_used) | {"vehicle"}},
                          escalation_reasons=plan.escalation_reasons,
                          decision="auto_send" if decision.auto_send else "draft_only", decision_reasons=decision.reasons,
                          generated_by=plan.generated_by, engine_version=ENGINE_VERSION, status="pending_review")
    db.add(draft)
    db.flush()
    log(db, "conversation.draft_created", client_id=client.id, entity_type="response_draft", entity_id=draft.id,
        decision=draft.decision, reasons=decision.reasons[:10], intents=plan.intents, confidence=plan.confidence,
        generated_by=plan.generated_by)
    if decision.auto_send:
        draft.status = "auto_sent"
        queue_outbound(db, conv, plan.body, sender_type="ai_auto", draft=draft, idempotency_key=f"auto:{draft.id}")
    if plan.escalation_reasons:
        escalation.open_or_update(db, conv, plan.escalation_reasons, msg)
    elif not decision.auto_send:
        first = sum(1 for m in history if m.direction == "inbound") == 1
        summary = build_summary(db, conv)
        # A new lead is also e-mailed to the dealership (gated client e-mail: SIMULATED unless authorized). The e-mail
        # carries the summary but no customer contact details; staff open the conversation for those.
        notify.notify(db, client.id, kind="new_lead" if first else "new_inquiry",
                      subject=f"{'New lead' if first else 'Reply waiting'}: {fs.vehicle_title or 'general question'} "
                              f"({'ES' if lang == 'es' else 'EN'})",
                      body="A draft reply is ready for review.\n\n" + summary_text(summary),
                      link=conversation_link(client.id, conv.id), assigned_user_id=conv.assigned_user_id,
                      dedupe_key=f"draft:{draft.id}")
        if first and conv.summary_delivered_at is None:
            conv.summary_delivered_at = utcnow()
    build_summary(db, conv)
    return {"draft_id": draft.id, "auto_send": decision.auto_send, "escalations": plan.escalation_reasons,
            "appointment_id": appt.id if appt else None}


@handler(JOB)
def process_job(db: Session, payload: dict) -> dict:
    return process_message(db, payload["message_id"])


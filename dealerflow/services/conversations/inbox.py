"""Inbound pipeline: ingest (dedupe, link contact/vehicle) → analyze → draft → decide → send/escalate → summarize."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import utcnow
from ...jobs import enqueue, handler
from ...models import (
    Client,
    Conversation,
    CustomerContact,
    CustomerIdentity,
    CustomerLead,
    ResponseDraft,
    Vehicle,
    WebhookEvent,
)
from ...models import ConversationMessage as Message
from . import escalation, notifications
from .appointments import request_from_conversation
from .automation_policy import decide, get_settings_row
from .channels import TWILIO_SMS
from .common import local_now, record
from .outbound import mark_opted_out, queue_outbound
from .responder import analysis as A
from .responder.engine import ENGINE_VERSION, R_HUMAN, compose, llm_rephrase
from .responder.facts import build_fact_sheet, resolve_vehicle
from .responder.language import detect_language
from .summaries import build_summary, conversation_link


@dataclass
class IngestResult:
    message: Message
    conversation: Conversation
    created: bool


def record_event(db: Session, provider: str, event_id: str, payload: dict, client_id: int | None) -> bool:
    """Store a raw provider event once in Phase 4's ``webhook_events`` (identifiers only, never message text).
    Returns False if it was already received (duplicate webhook)."""
    if db.scalar(select(WebhookEvent.id).where(WebhookEvent.provider == provider, WebhookEvent.event_id == event_id)):
        return False
    try:
        with db.begin_nested():
            db.add(WebhookEvent(provider=provider, event_id=event_id[:200], event_type="customer_message",
                                payload={**payload, "client_id": client_id}, status="processed", processed_at=utcnow()))
            db.flush()
    except IntegrityError:
        return False
    return True


def _contact_for(db: Session, client_id: int, channel: str, sender_id: str, hints: dict) -> CustomerContact:
    ident = db.scalar(select(CustomerIdentity).where(CustomerIdentity.client_id == client_id,
                                                     CustomerIdentity.channel == channel,
                                                     CustomerIdentity.external_id == sender_id))
    if ident:
        contact = ident.contact
    else:
        contact = None
        if hints.get("email"):
            contact = db.scalar(select(CustomerContact).where(CustomerContact.client_id == client_id,
                                                              CustomerContact.email == hints["email"].lower()))
        if contact is None:
            contact = CustomerContact(client_id=client_id, contact_source=channel)
            db.add(contact)
            db.flush()
        db.add(CustomerIdentity(client_id=client_id, contact_id=contact.id, channel=channel, external_id=sender_id))
    if hints.get("name") and not contact.display_name:
        contact.display_name = hints["name"][:200]
    if hints.get("email") and not contact.email:
        contact.email = hints["email"].lower()[:200]
    if hints.get("phone") and not contact.phone:
        contact.phone = hints["phone"][:40]
    if channel == TWILIO_SMS and not contact.phone:
        contact.phone = sender_id
    return contact


def ingest_inbound(db: Session, *, client: Client, channel: str, external_conversation_id: str, text: str,
                   provider_message_id: str | None, source_timestamp: datetime | None = None,
                   contact_hints: dict | None = None, vehicle_id: int | None = None,
                   simulated: bool = False) -> IngestResult:
    """Store an inbound customer message once and queue it for processing. ``simulated`` marks staff-simulated or
    demo traffic (always true for a SIMULATED client) so it is never reported as a real inquiry."""
    simulated = simulated or bool(client.simulated)
    if provider_message_id:
        existing = db.scalar(select(Message).where(Message.channel == channel,
                                                   Message.provider_message_id == provider_message_id))
        if existing:
            if existing.client_id != client.id:  # same provider id under another tenant → never merge
                raise ValueError("provider message id belongs to another dealership")
            return IngestResult(existing, existing.conversation, False)
    if vehicle_id is not None:
        v = db.get(Vehicle, vehicle_id)
        if v is None or v.client_id != client.id:
            vehicle_id = None  # ignore cross-tenant or unknown vehicle references
    contact = _contact_for(db, client.id, channel, external_conversation_id, contact_hints or {})
    conv = db.scalar(select(Conversation).where(Conversation.client_id == client.id,
                                                Conversation.channel == channel,
                                                Conversation.external_conversation_id == external_conversation_id))
    created_conv = conv is None
    if conv is None:
        lang, _ = detect_language(text)
        conv = Conversation(client_id=client.id, channel=channel, external_conversation_id=external_conversation_id,
                            contact_id=contact.id, vehicle_id=vehicle_id, preferred_language=lang, simulated=simulated)
        db.add(conv)
        db.flush()
        lead = CustomerLead(client_id=client.id, contact_id=contact.id, vehicle_id=vehicle_id, source=channel)
        db.add(lead)
        db.flush()
        conv.lead_id = lead.id
    elif vehicle_id and not conv.vehicle_id:
        conv.vehicle_id = vehicle_id
    sensitive = A.detect_sensitive(text)  # detect before redaction so the signal is not lost
    msg = Message(conversation_id=conv.id, client_id=client.id, channel=channel, direction="INBOUND",
                  sender_type="CUSTOMER", body=A.redact(text)[:10000], provider_message_id=provider_message_id,
                  source_timestamp=source_timestamp, delivery_status="RECEIVED",
                  analysis={"sensitive_detected": sensitive} if sensitive else {})
    try:
        with db.begin_nested():
            db.add(msg)
            db.flush()
    except IntegrityError:  # concurrent duplicate delivery
        existing = db.scalar(select(Message).where(Message.channel == channel,
                                                   Message.provider_message_id == provider_message_id))
        return IngestResult(existing, existing.conversation, False)
    conv.last_inbound_at = utcnow()
    if conv.status in ("CLOSED", "AWAITING_CUSTOMER", "OPEN"):
        conv.status = "AWAITING_STAFF"
    record(db, "message.received", client_id=client.id, entity_type="message", entity_id=msg.id, channel=channel,
           new_conversation=created_conv, simulated=simulated or None)
    enqueue(db, "conversations.process_message", f"conv-process:{msg.id}", {"message_id": msg.id})
    return IngestResult(msg, conv, True)


class _GatewayLLM:
    """Adapter from the shared AI gateway (``integrations.llm.complete``) to the engine's ``complete`` interface.

    Used only when DF_CONVERSATION_AI_ENABLED and the shared DF_AI_ENABLED switch are both on. Token usage is
    recorded per client by the gateway; the gateway also passes the emergency-stop gate.
    """

    def __init__(self, db: Session, client_id: int):
        self.db, self.client_id = db, client_id

    def complete(self, *, system: str, prompt: str, max_tokens: int = 800) -> tuple[str | None, float]:
        from ...integrations import llm

        try:
            res = llm.complete(self.db, system=system, prompt=prompt, feature="conversation_rephrase",
                               client_id=self.client_id, max_tokens=max_tokens)
        except Exception:  # noqa: BLE001 - AI is optional: any failure keeps the deterministic draft
            return None, 0.0
        return (None if res.refused else res.text), res.estimated_cost


def _llm(db: Session, client_id: int) -> _GatewayLLM | None:
    s = get_settings()
    return _GatewayLLM(db, client_id) if s.conversation_ai_enabled and s.ai_available else None


def process_message(db: Session, message_id: int) -> dict:
    msg = db.get(Message, message_id)
    if msg is None or msg.direction != "INBOUND":
        raise ValueError("message not found")  # not retryable
    if (msg.analysis or {}).get("processed"):
        return {"skipped": "already processed"}
    conv = db.get(Conversation, msg.conversation_id)
    db.expire(conv, ["messages"])  # make sure the message history is current
    client = db.get(Client, conv.client_id)
    settings = get_settings_row(db, client.id)
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
        notifications.notify(db, client.id, kind="SYSTEM", subject=f"Customer opted out (conversation #{conv.id})",
                             body="The customer asked not to be contacted. Automation is stopped for this conversation.",
                             link=conversation_link(conv),
                             assigned_user_id=conv.assigned_user_id, immediate=False, dedupe_key=f"optout:{conv.id}")
        build_summary(db, conv)
        return {"opt_out": True}
    if an.human_request:
        conv.human_requested = True

    fs = build_fact_sheet(db, client, settings, lang, vehicle, ambiguous)
    prior_out = [m for m in conv.messages if m.direction == "OUTBOUND"]
    offered = any("test drive" in m.body.lower() or "prueba de manejo" in m.body.lower() for m in prior_out)
    plan = compose(an, fs, msg.body, contact_name=(contact.display_name.split()[0] if contact and contact.display_name else None),
                   has_contact_method=bool(contact and (contact.phone or contact.email) or conv.channel == TWILIO_SMS),
                   already_offered_test_drive=offered, is_first_reply=not prior_out, now_local=now_local)
    if plan.escalation_reasons and R_HUMAN in plan.escalation_reasons:
        conv.human_requested = True

    llm = _llm(db, client.id)
    if llm is not None and not plan.no_reply and not plan.escalation_reasons:
        text, _cost = llm_rephrase(llm, plan, fs)  # usage/cost already recorded per client by the gateway
        if text:
            plan.body, plan.generated_by = text, "template+llm"

    if A.CANCEL_APPOINTMENT in an.intents:
        from .appointments import customer_change_requested

        customer_change_requested(db, conv, msg.body)
    appt = None
    if plan.appointment_request is not None and plan.appointment_request_in_hours is not False:
        appt = request_from_conversation(db, conv, plan.appointment_request,
                                         name=contact.display_name if contact else None,
                                         phone=contact.phone if contact else None,
                                         email=contact.email if contact else None)

    for old in db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id,
                                                      ResponseDraft.status == "PENDING_REVIEW")):
        old.status = "SUPERSEDED"
    decision = decide(db, conv, plan, fs)
    draft = ResponseDraft(conversation_id=conv.id, client_id=client.id, in_reply_to_message_id=msg.id,
                          body=plan.body, language=lang, confidence=plan.confidence, intents=plan.intents,
                          facts_used=plan.facts_used, unresolved=plan.unresolved,
                          fact_snapshot={f.key: f.value for f in fs.facts
                                         if f.key in set(plan.facts_used) | {"vehicle"}},
                          escalation_reasons=plan.escalation_reasons, decision="AUTO_SEND" if decision.auto_send else "DRAFT_ONLY",
                          decision_reasons=decision.reasons, generated_by=plan.generated_by, engine_version=ENGINE_VERSION,
                          status="PENDING_REVIEW")
    db.add(draft)
    db.flush()
    record(db, "draft.created", client_id=client.id, entity_type="response_draft", entity_id=draft.id,
           decision=draft.decision, reasons=decision.reasons, intents=plan.intents, confidence=plan.confidence)
    if decision.auto_send:
        draft.status = "AUTO_SENT"
        queue_outbound(db, conv, plan.body, sender_type="AI_AUTO", draft=draft, idempotency_key=f"auto:{draft.id}")
    if plan.escalation_reasons:
        escalation.open_or_update(db, conv, plan.escalation_reasons, msg)
    elif not decision.auto_send:
        first_inbound = sum(1 for m in conv.messages if m.direction == "INBOUND") == 1
        notifications.notify(db, client.id, kind="NEW_INQUIRY",
                             subject=f"{'New inquiry' if first_inbound else 'Reply waiting'}: "
                                     f"{fs.vehicle_title or 'general question'} ({'ES' if lang == 'es' else 'EN'})",
                             body=f"A draft reply is ready for review.\n\nCustomer: {msg.body[:500]}\n\nDraft: {plan.body}",
                             link=conversation_link(conv), assigned_user_id=conv.assigned_user_id,
                             dedupe_key=f"draft:{draft.id}")
    build_summary(db, conv)
    return {"draft_id": draft.id, "auto_send": decision.auto_send, "escalations": plan.escalation_reasons,
            "appointment_id": appt.id if appt else None}


@handler("conversations.process_message")
def process_message_job(db: Session, payload: dict) -> dict:
    return process_message(db, int(payload["message_id"]))

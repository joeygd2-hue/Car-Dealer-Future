"""Agency-side inquiry tools on top of the Phase 3 customer-response pipeline.

Phase 4 shipped an interim stand-in here (fixed templates over interim tables). The Phase 3 port replaces it:
a simulated inquiry now goes through the real pipeline (ingest → analyze → grounded draft → policy), the draft is
the engine's, sending goes through the send gate (SANDBOX unless live delivery is authorized), and the lead summary
is the Phase 3 summary. The JSON API contract of ``/api/v1/clients/{cid}/inquiries/simulated`` and the
draft/approve endpoints is kept; ``message_id`` in those responses is the draft id.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import utcnow
from ..integrations import customer_channels as channels
from ..models import Client, User
from ..models_customer import CustomerConversation, ResponseDraft
from ..models_dealer import Vehicle
from . import client_email

# Kept for the API's "template" field. The engine composes the reply from verified facts; these keys only name
# the intent the agency asked for and are checked against the draft's detected intents.
APPROVED_RESPONSES = {"availability": "availability", "test_drive": "test_drive"}
CHANNEL_ALIASES = {"web_form": channels.WEBSITE_CHAT, "website_chat": channels.WEBSITE_CHAT, "sms": channels.TWILIO_SMS,
                   "messenger": channels.META_MESSENGER, "marketplace": channels.MARKETPLACE_ASSISTED, "adf": channels.ADF_LEAD}


def receive_inquiry(db: Session, client: Client, *, channel: str, customer_name: str | None, customer_contact: str | None,
                    body: str, language: str, vehicle: Vehicle | None, provider_message_id: str,
                    simulated: bool = True) -> CustomerConversation:
    from .customer.processing import ingest_inbound, process_message

    if vehicle is not None and vehicle.client_id != client.id:
        raise ValueError("Vehicle belongs to another dealership")
    hints = {"name": customer_name}
    if customer_contact:
        hints["email" if "@" in customer_contact else "phone"] = customer_contact
    ch = CHANNEL_ALIASES.get(channel, channel if channel in channels.CUSTOMER_CHANNELS else channels.WEBSITE_CHAT)
    res = ingest_inbound(db, dealership=client, channel=ch, external_conversation_id=f"sim:{provider_message_id}", text=body,
                         provider_message_id=provider_message_id, contact_hints=hints,
                         vehicle_id=vehicle.id if vehicle else None, simulated=simulated)
    if res.created:
        if language in ("en", "es") and res.conversation.preferred_language != language:
            res.conversation.preferred_language = language
        process_message(db, res.message.id)  # synchronous so the caller sees the draft
    db.flush()
    return res.conversation


def draft_response(db: Session, conv: CustomerConversation, template_key: str) -> ResponseDraft:
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id, ResponseDraft.status == "PENDING_REVIEW")
                      .order_by(ResponseDraft.id.desc()))
    if draft is None:
        raise ValueError("No reply is awaiting review for this conversation (opted out, or already handled)")
    return draft


def approve_and_send_response(db: Session, draft: ResponseDraft, actor: User) -> ResponseDraft:
    """Staff approval: queue the engine's draft as a staff reply (sandbox unless live delivery is authorized)."""
    from .customer.outbound import queue_outbound

    conv = db.get(CustomerConversation, draft.conversation_id)
    if draft.status != "PENDING_REVIEW":
        raise ValueError(f"Draft is {draft.status}, not awaiting review")
    if conv.opted_out:
        raise ValueError("The customer opted out; no reply may be sent")
    draft.status, draft.reviewed_by_id, draft.reviewed_at = "SENT", actor.id, utcnow()
    queue_outbound(db, conv, draft.body, sender_type="STAFF", user_id=actor.id, draft=draft,
                   idempotency_key=f"approve:{draft.id}")
    audit.event(db, "conversation.response_approved", actor_id=actor.id, client_id=conv.client_id,
                entity_type="response_draft", entity_id=draft.id)
    return draft


def summarize_and_deliver(db: Session, conv: CustomerConversation) -> str:
    """Phase 3 lead summary, emailed once to the dealership's contact address (sandboxed unless authorized)."""
    from .customer.summaries import build_summary, summary_text

    client = db.get(Client, conv.client_id)
    text = summary_text(build_summary(db, conv))
    if client.contact_email and not (conv.summary or {}).get("delivered_at"):
        outcome = client_email.send(db, client, to=client.contact_email, subject=f"Lead summary #{conv.id}", body=text,
                                    key=f"conv_summary:{conv.id}")
        if outcome in ("sent", "simulated"):
            conv.summary = {**(conv.summary or {}), "delivered_at": utcnow().isoformat(timespec="seconds"), "delivery": outcome}
    return text


def summary_delivered(conv: CustomerConversation) -> bool:
    return bool((conv.summary or {}).get("delivered_at"))

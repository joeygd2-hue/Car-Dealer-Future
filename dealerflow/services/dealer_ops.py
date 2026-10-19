"""INTERIM Phase 3 workflows needed by reporting and end-to-end validation.

SCOPE NOTE: customer conversations with approved bilingual responses and summaries, on the interim
tables in models_interim.py. They stand in for the Phase 3 customer-response engine until it is ported.
Inventory, listings and publishing are Phase 2 (services/inventory, services/listings,
services/publishing); the interim versions that used to live here were removed at integration.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import utcnow
from ..models import Appointment, Client, Conversation, ConversationMessage, User, Vehicle
from . import client_email

APPROVED_RESPONSES = {
    "availability": {
        "en": "Thanks for your interest in the {vehicle}. A member of our team will confirm availability and reach out shortly.",
        "es": "Gracias por su interés en el {vehicle}. Un miembro de nuestro equipo confirmará la disponibilidad y se comunicará pronto.",
    },
    "test_drive": {
        "en": "We'd be happy to set up a test drive of the {vehicle}. What day and time works for you?",
        "es": "Con gusto le agendamos una prueba de manejo del {vehicle}. ¿Qué día y hora le conviene?",
    },
}
ESCALATE_RE = re.compile(r"\b(lawyer|complaint|accident|warranty claim|credit score|financing approval|abogado|queja|accidente|garantía)\b", re.I)


def receive_inquiry(db: Session, client: Client, *, channel: str, customer_name: str | None, customer_contact: str | None,
                    body: str, language: str, vehicle: Vehicle | None, provider_message_id: str, simulated: bool = True) -> Conversation:
    existing = db.scalar(select(ConversationMessage).where(ConversationMessage.provider_message_id == provider_message_id))
    if existing:
        return db.get(Conversation, existing.conversation_id)
    now = utcnow()
    conv = Conversation(client_id=client.id, channel=channel, customer_name=customer_name, customer_contact=customer_contact,
                        language=language if language in ("en", "es") else "en", vehicle_id=vehicle.id if vehicle else None, first_inbound_at=now, simulated=simulated)
    db.add(conv)
    db.flush()
    db.add(ConversationMessage(client_id=client.id, conversation_id=conv.id, direction="inbound", body=body, language=conv.language, provider_message_id=provider_message_id))
    if ESCALATE_RE.search(body):
        conv.status, conv.escalated_at = "escalated", now
    if re.search(r"test drive|prueba de manejo", body, re.I):
        conv.lead_qualified = True
        db.add(Appointment(client_id=client.id, conversation_id=conv.id, kind="test_drive"))
    db.flush()
    return conv


def draft_response(db: Session, conv: Conversation, template_key: str) -> ConversationMessage:
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    vname = " ".join(str(x) for x in (vehicle.year, vehicle.make, vehicle.model) if x) if vehicle else ("vehicle" if conv.language == "en" else "vehículo")
    text = APPROVED_RESPONSES[template_key][conv.language].format(vehicle=vname)
    m = ConversationMessage(client_id=conv.client_id, conversation_id=conv.id, direction="outbound", body=text, language=conv.language, status="pending_approval")
    db.add(m)
    db.flush()
    return m


def approve_and_send_response(db: Session, msg: ConversationMessage, actor: User) -> ConversationMessage:
    """Human-approved send. Delivered via sandbox unless the client's messaging integration is live."""
    conv = db.get(Conversation, msg.conversation_id)
    if conv.status == "escalated":
        raise ValueError("Escalated conversations must be handled by the dealership")
    # No customer-messaging channel is implemented yet, so delivery is SIMULATED (Phase 3 adds real channels).
    msg.status, msg.approved_by_id, msg.sent_at = "sent_simulated", actor.id, utcnow()
    conv.first_response_at = conv.first_response_at or msg.sent_at
    audit.record(db, "conversation.response_sent", actor=actor, entity=msg, details={"delivery": "simulated"})
    return msg


def summarize_and_deliver(db: Session, conv: Conversation) -> str:
    """Rule-based summary sent to the dealership's contact email (sandboxed unless authorized)."""
    client = db.get(Client, conv.client_id)
    msgs = db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conv.id).order_by(ConversationMessage.id)).all()
    first = next((m.body for m in msgs if m.direction == "inbound"), "")
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    summary = (
        f"Channel: {conv.channel}. Language: {conv.language}. "
        + (f"Vehicle: {vehicle.year} {vehicle.make} {vehicle.model} (VIN {vehicle.vin}). " if vehicle else "")
        + f"Customer asked: \"{first[:300]}\". "
        + ("Test-drive requested. " if conv.lead_qualified else "")
        + ("ESCALATED — needs dealership follow-up. " if conv.status == "escalated" else "")
        + f"Messages: {len(msgs)}."
    )
    conv.summary = summary
    if client.contact_email and conv.summary_delivered_at is None:
        outcome = client_email.send(db, client, to=client.contact_email, subject=f"Lead summary #{conv.id}", body=summary,
                                    key=f"conv_summary:{conv.id}")
        if outcome in ("sent", "simulated"):
            conv.summary_delivered_at = utcnow()
    return summary

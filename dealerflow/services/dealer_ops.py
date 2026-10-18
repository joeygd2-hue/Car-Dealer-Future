"""Minimal Phase 2/3 workflows needed by reporting and end-to-end validation.

SCOPE NOTE: this is shared scaffolding, not the full Phase 2/3 feature set.
Inventory CSV import, deterministic (template) EN/ES listing generation with
approval, publish-status recording, and customer conversations with approved
bilingual responses and summaries. Phase 2/3 should extend these functions and
the models in app/models/dealer_ops.py rather than create parallel ones.
"""
from __future__ import annotations

import csv
import io
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import utcnow
from ..models import Appointment, Client, Conversation, ConversationMessage, InventoryImport, Listing, PublishAttempt, User, Vehicle
from . import client_email
from .onboarding import automation_enabled

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# Channels with a supported, implemented publishing API. Everything else is manual.
SUPPORTED_PUBLISH_CHANNELS: set[str] = set()


def import_inventory_csv(db: Session, client: Client, content: str, actor: User | None) -> InventoryImport:
    imp = InventoryImport(client_id=client.id, source="csv")
    db.add(imp)
    db.flush()
    errors = []
    for i, row in enumerate(csv.DictReader(io.StringIO(content)), start=2):
        imp.rows_total += 1
        vin = (row.get("vin") or "").strip().upper()
        if not VIN_RE.match(vin):
            imp.rows_failed += 1
            errors.append(f"line {i}: invalid VIN")
            continue
        try:
            year = int(row["year"]) if row.get("year") else None
            mileage = int(row["mileage"].replace(",", "")) if row.get("mileage") else None
            price = int(round(float(row["price"].replace(",", "").replace("$", "")) * 100)) if row.get("price") else None
        except ValueError:
            imp.rows_failed += 1
            errors.append(f"line {i}: invalid number")
            continue
        if price is not None and price < 0:
            imp.rows_failed += 1
            errors.append(f"line {i}: negative price")
            continue
        v = db.scalar(select(Vehicle).where(Vehicle.client_id == client.id, Vehicle.vin == vin))
        fields = dict(year=year, make=(row.get("make") or "").strip() or None, model=(row.get("model") or "").strip() or None,
                      trim=(row.get("trim") or "").strip() or None, mileage=mileage, price_cents=price,
                      stock_number=(row.get("stock_number") or "").strip() or None, import_id=imp.id)
        if v is None:
            db.add(Vehicle(client_id=client.id, vin=vin, **fields))
            imp.rows_created += 1
        else:
            for k, val in fields.items():
                setattr(v, k, val)
            imp.rows_updated += 1
    imp.errors, imp.finished_at = errors[:200], utcnow()
    audit.record(db, "inventory.imported", actor=actor, entity=imp, details={"created": imp.rows_created, "failed": imp.rows_failed})
    return imp


def generate_listings(db: Session, vehicle: Vehicle) -> list[Listing]:
    """Deterministic EN/ES listing drafts from verified vehicle fields only. Requires human approval."""
    out = []
    name = " ".join(str(x) for x in (vehicle.year, vehicle.make, vehicle.model, vehicle.trim) if x)
    price = f"${vehicle.price_cents / 100:,.0f}" if vehicle.price_cents is not None else None
    miles = f"{vehicle.mileage:,}" if vehicle.mileage is not None else None
    texts = {
        "en": (f"{name}" + (f" — {price}" if price else ""),
               f"{name}. " + (f"{miles} miles. " if miles else "") + f"VIN {vehicle.vin}. Contact us to schedule a test drive."),
        "es": (f"{name}" + (f" — {price}" if price else ""),
               f"{name}. " + (f"{miles} millas. " if miles else "") + f"VIN {vehicle.vin}. Contáctenos para agendar una prueba de manejo."),
    }
    for lang, (title, body) in texts.items():
        existing = db.scalar(select(Listing).where(Listing.vehicle_id == vehicle.id, Listing.language == lang))
        if existing:
            out.append(existing)
            continue
        listing = Listing(client_id=vehicle.client_id, vehicle_id=vehicle.id, language=lang, title=title[:300], body=body, generated_by="template")
        db.add(listing)
        out.append(listing)
    db.flush()
    return out


def approve_listing(db: Session, listing: Listing, actor: User) -> None:
    listing.status, listing.approved_by_id, listing.approved_at = "approved", actor.id, utcnow()
    audit.record(db, "listing.approved", actor=actor, entity=listing)


def publish_listing(db: Session, listing: Listing, channel: str) -> PublishAttempt:
    if listing.status != "approved":
        raise ValueError("Only approved listings can be published")
    if channel in SUPPORTED_PUBLISH_CHANNELS and automation_enabled(db, listing.client_id, "listing_publish"):
        raise NotImplementedError("No channel publishing API is implemented yet")
    attempt = PublishAttempt(client_id=listing.client_id, listing_id=listing.id, channel=channel, status="manual_required",
                             note=f"No supported publishing integration for '{channel}'. Post manually and record the URL.")
    db.add(attempt)
    db.flush()
    return attempt


def confirm_manual_publication(db: Session, attempt: PublishAttempt, url: str, actor: User) -> None:
    attempt.status, attempt.external_url, attempt.confirmed_at = "published_confirmed", url, utcnow()
    attempt.note = (attempt.note or "") + f" Confirmed manually by {actor.email}."
    audit.record(db, "listing.publication_confirmed", actor=actor, entity=attempt)


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

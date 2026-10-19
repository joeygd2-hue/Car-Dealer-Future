"""Public lead capture: website chat / inquiry form and ADF/XML lead intake (ported from Phase 3 ``web/public.py``).

Both paths identify the dealership by a per-client key (never by an id in the URL) and call ``ingest_inbound``,
which deduplicates messages and links the contact, lead, conversation and vehicle.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Client
from ...models_conversations import ADF_LEAD, WEBSITE_CHAT
from ...models_dealer import Vehicle
from .adf import parse_adf
from .inbox import IngestResult, ingest_inbound

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


def client_by_key(db: Session, *, chat_key: str | None = None, adf_key: str | None = None) -> Client | None:
    if not (chat_key or adf_key):
        return None
    col = Client.public_chat_key if chat_key else Client.adf_intake_key
    c = db.scalar(select(Client).where(col == (chat_key or adf_key)))
    if c is None or c.status in ("paused", "offboarded"):
        return None
    return c


def _clean_hints(hints: dict) -> dict:
    out = {}
    if hints.get("name"):
        out["name"] = str(hints["name"]).strip()[:100] or None
    email = str(hints.get("email") or "").strip().lower()
    if email and EMAIL_RE.match(email):
        out["email"] = email
    phone = re.sub(r"[^\d+()\- ]", "", str(hints.get("phone") or ""))[:40].strip()
    if len(re.sub(r"\D", "", phone)) >= 7:
        out["phone"] = phone
    return out


def _vehicle_by_stock(db: Session, client: Client, stock: str | None) -> int | None:
    stock = (stock or "").strip()[:50]
    if not stock:
        return None
    v = db.scalar(select(Vehicle).where(Vehicle.client_id == client.id, (Vehicle.stock_number == stock) | (Vehicle.vin == stock.upper())))
    return v.id if v else None


def submit_chat(db: Session, client: Client, *, visitor: str, text: str, client_message_id: str | None, hints: dict,
                vehicle_stock: str | None, simulated: bool = False) -> IngestResult:
    visitor = re.sub(r"[^A-Za-z0-9_-]", "", visitor)[:64]
    text = text.strip()[:2000]
    if len(visitor) < 8 or not text:
        raise ValueError("visitor_id (8+ letters/digits) and text are required")
    msg_id = re.sub(r"[^A-Za-z0-9_-]", "", client_message_id or "")[:64] or uuid.uuid4().hex
    return ingest_inbound(db, client=client, channel=WEBSITE_CHAT, external_conversation_id=f"web:{visitor}", text=text,
                          provider_message_id=f"{visitor}:{msg_id}", contact_hints=_clean_hints(hints),
                          vehicle_id=_vehicle_by_stock(db, client, vehicle_stock), simulated=simulated or client.simulated)


def submit_adf(db: Session, client: Client, xml: str) -> list[IngestResult]:
    out = []
    for lead in parse_adf(xml):
        sender = (lead.email or lead.phone or lead.lead_id or "unknown").lower()[:180]
        text = lead.comments or " ".join(x for x in ("Interested in", lead.year, lead.make, lead.model) if x)
        out.append(ingest_inbound(
            db, client=client, channel=ADF_LEAD, external_conversation_id=f"adf:{sender}", text=text[:4000],
            provider_message_id=f"adf:{lead.lead_id}"[:300], source_timestamp=lead.request_date,
            contact_hints=_clean_hints({"name": lead.name, "email": lead.email, "phone": lead.phone}),
            vehicle_id=_vehicle_by_stock(db, client, lead.vin or lead.stock), simulated=client.simulated))
    return out

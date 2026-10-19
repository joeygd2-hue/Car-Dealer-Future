"""Unauthenticated customer endpoints (Phase 3): website chat API + test page, and ADF/XML lead intake.

- Chat is addressed by the dealership's unguessable ``public_chat_key``; paused/cancelled clients get 404.
- Per-IP and per-visitor limits use the shared database counters (multi-process safe).
- CORS is granted only to the dealership's own website origin (and this app's base URL).
- A visitor only ever sees replies that were actually delivered (status SENT). SIMULATED replies stay internal.
- ADF intake needs a per-connection bearer token; only its SHA-256 is stored (connection ``config.token_sha256``).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db, utcnow
from ..models import Client, Conversation, CustomerContact, IntegrationConnection, Message, Vehicle
from ..services.conversations import channels, rate_limits, runtime
from ..services.conversations.adf import parse_adf
from ..services.conversations.processing import ingest_inbound, record_event
from .deps import render

router = APIRouter()


def _cors(request: Request, client: Client, resp):
    origin = request.headers.get("origin")
    allowed = {get_settings().base_url.rstrip("/")}
    if client.website_origin:
        allowed.add(client.website_origin)
    if origin and origin.rstrip("/") in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _client_by_key(db: Session, key: str) -> Client:
    c = db.scalar(select(Client).where(Client.public_chat_key == key))
    if c is None or c.status in ("paused", "cancelled"):
        raise HTTPException(404, "Not found")
    return c


@router.options("/api/chat/{key}/messages")
def chat_preflight(key: str, request: Request, db: Session = Depends(get_db)):
    return _cors(request, _client_by_key(db, key), PlainTextResponse(""))


@router.post("/api/chat/{key}/messages")
async def chat_post(key: str, request: Request, db: Session = Depends(get_db)):
    client = _client_by_key(db, key)
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "invalid JSON")
    visitor = str(data.get("visitor_id", ""))[:64]
    text = str(data.get("text", "")).strip()[:2000]
    if not visitor or len(visitor) < 8 or not text:
        raise HTTPException(400, "visitor_id and text are required")
    ip = request.client.host if request.client else "?"
    if not (rate_limits.try_acquire(db, f"chat:ip:{client.id}:{ip}", 30, 60)
            and rate_limits.try_acquire(db, f"chat:visitor:{client.id}:{visitor}", 8, 60)):
        db.commit()
        return _cors(request, client, JSONResponse({"error": "Too many messages. Please wait a moment."}, 429))
    client_msg_id = str(data.get("client_message_id") or uuid.uuid4())[:64]
    vehicle_id = None
    stock = str(data.get("vehicle_stock") or "")[:50]
    if stock:
        v = db.scalar(select(Vehicle).where(Vehicle.client_id == client.id, Vehicle.stock_number == stock))
        vehicle_id = v.id if v else None
    hints = {"name": str(data.get("name") or "")[:100] or None}
    try:
        res = ingest_inbound(db, dealership=client, channel=channels.WEBSITE_CHAT, external_conversation_id=f"web:{visitor}",
                             text=text, provider_message_id=f"{client.id}:{visitor}:{client_msg_id}", contact_hints=hints,
                             vehicle_id=vehicle_id)
    except ValueError:
        raise HTTPException(409, "duplicate message id") from None
    if data.get("sms_consent") is True and data.get("phone"):
        contact = db.get(CustomerContact, res.conversation.contact_id)
        contact.phone = str(data["phone"])[:40]
        contact.sms_consent = True
        contact.sms_consent_source = (f"website chat checkbox {utcnow().isoformat()} ip={ip}: "
                                      f"{str(data.get('consent_text', ''))[:300]}")
    db.commit()
    return _cors(request, client, JSONResponse({"ok": True, "message_id": res.message.id, "duplicate": not res.created}))


@router.get("/api/chat/{key}/messages")
def chat_poll(key: str, visitor_id: str, request: Request, after_id: int = 0, db: Session = Depends(get_db)):
    client = _client_by_key(db, key)
    conv = db.scalar(select(Conversation).where(Conversation.client_id == client.id, Conversation.channel == channels.WEBSITE_CHAT,
                                                Conversation.external_conversation_id == f"web:{visitor_id[:64]}"))
    out = []
    if conv:
        for m in db.scalars(select(Message).where(Message.conversation_id == conv.id, Message.client_id == client.id,
                                                  Message.id > after_id, Message.direction == "OUTBOUND",
                                                  Message.delivery_status == "SENT").order_by(Message.id)):
            out.append({"id": m.id, "text": m.body, "at": m.created_at.isoformat()})
    return _cors(request, client, JSONResponse({"messages": out}))


@router.get("/chat/{key}")
def chat_test_page(key: str, request: Request, db: Session = Depends(get_db)):
    return render(request, "chat_page.html", None, dealership=_client_by_key(db, key))


@router.post("/api/leads/adf/{connection_id}")
async def adf_intake(connection_id: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(IntegrationConnection, connection_id)
    auth = request.headers.get("authorization", "")
    expected = (conn.config or {}).get("token_sha256") if conn else None
    given = hashlib.sha256(auth.removeprefix("Bearer ").encode()).hexdigest()
    if (conn is None or conn.kind != runtime.CONNECTION_KIND or conn.provider != channels.ADF_LEAD or not expected
            or not hmac.compare_digest(given, expected)):
        raise HTTPException(401, "unauthorized")
    client = db.get(Client, conn.client_id)
    if conn.status == "disabled" or client is None or client.status in ("paused", "cancelled"):
        raise HTTPException(403, "lead intake is disabled for this connection")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "payload too large")
    try:
        leads = parse_adf(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - malformed/hostile XML
        raise HTTPException(400, "invalid ADF/XML") from None
    created = 0
    for lead in leads:
        if not record_event(db, "ADF", f"{conn.id}:{lead.lead_id}", {"lead_id": lead.lead_id}, client.id):
            continue
        vehicle = None
        if lead.vin:
            vehicle = db.scalar(select(Vehicle).where(Vehicle.client_id == client.id, Vehicle.vin == lead.vin))
        if vehicle is None and lead.stock:
            vehicle = db.scalar(select(Vehicle).where(Vehicle.client_id == client.id, Vehicle.stock_number == lead.stock))
        text = lead.comments or f"Lead from {lead.provider or 'ADF'} about {' '.join(x for x in [lead.year, lead.make, lead.model] if x)}"
        ingest_inbound(db, dealership=client, channel=channels.ADF_LEAD,
                       external_conversation_id=f"adf:{lead.email or lead.phone or lead.lead_id}", text=text,
                       provider_message_id=f"{conn.id}:{lead.lead_id}", source_timestamp=lead.request_date,
                       contact_hints={"name": lead.name, "email": lead.email, "phone": lead.phone},
                       vehicle_id=vehicle.id if vehicle else None)
        created += 1
    db.commit()
    return {"ok": True, "leads": len(leads), "created": created}

"""Phase 3 unauthenticated endpoints: website chat API + widget page, Meta and Twilio webhooks, Meta data-deletion
callback and ADF/XML lead intake.

No session or CSRF applies here; each endpoint authenticates the caller its own way (public widget key with
per-IP/per-visitor DB rate limits, provider HMAC signatures, a per-connection bearer token for ADF). Raw provider
events are stored once (identifiers only) for de-duplication. Nothing here sends anything to a customer: replies
go through the outbound queue and every channel adapter is a sandbox in this build.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db, utcnow
from ..models import (
    Appointment,
    Client,
    Conversation,
    ConversationMessage,
    CustomerContact,
    CustomerIdentity,
    CustomerLead,
    Escalation,
    IntegrationConnection,
    ResponseDraft,
    Vehicle,
)
from ..services.conversations import rate_limits
from ..services.conversations.adf import parse_adf
from ..services.conversations.channels import (
    ADF_LEAD,
    META_MESSENGER,
    TWILIO_SMS,
    WEBSITE_CHAT,
    parse_meta_webhook,
    verify_meta_signature,
    verify_twilio_signature,
)
from ..services.conversations.common import record
from ..services.conversations.inbox import ingest_inbound, record_event
from .deps import render

router = APIRouter()


# --- website chat -----------------------------------------------------------------------------------------------


def _cors(request: Request, client: Client, resp):
    origin = request.headers.get("origin")
    allowed = {get_settings().base_url.rstrip("/")}
    if client.website:
        allowed.add(client.website.rstrip("/"))
    if origin and origin.rstrip("/") in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _client_by_key(db: Session, key: str) -> Client:
    c = db.scalar(select(Client).where(Client.public_chat_key == key))
    if c is None or c.status in ("cancelled", "paused"):
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
    if len(visitor) < 8 or not text:
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
    res = ingest_inbound(db, client=client, channel=WEBSITE_CHAT, external_conversation_id=f"web:{visitor}", text=text,
                         provider_message_id=f"{visitor}:{client_msg_id}", contact_hints=hints, vehicle_id=vehicle_id)
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
    """Replies the visitor may see: only messages a live channel actually delivered (SENT). SIMULATED replies stay
    inside the platform."""
    client = _client_by_key(db, key)
    conv = db.scalar(select(Conversation).where(Conversation.client_id == client.id, Conversation.channel == WEBSITE_CHAT,
                                                Conversation.external_conversation_id == f"web:{visitor_id[:64]}"))
    out = []
    if conv:
        for m in db.scalars(select(ConversationMessage).where(
                ConversationMessage.conversation_id == conv.id, ConversationMessage.id > after_id,
                ConversationMessage.direction == "OUTBOUND", ConversationMessage.delivery_status == "SENT")
                .order_by(ConversationMessage.id)):
            out.append({"id": m.id, "text": m.body, "at": m.created_at.isoformat()})
    return _cors(request, client, JSONResponse({"messages": out}))


@router.get("/chat/{key}")
def chat_page(key: str, request: Request, db: Session = Depends(get_db)):
    client = _client_by_key(db, key)
    return render(request, "chat_page.html", None, dealer=client, chat_key=key)


# --- Meta Messenger (inbound only) ------------------------------------------------------------------------------


@router.get("/webhooks/meta")
def meta_verify(request: Request):
    p = request.query_params
    token = get_settings().meta_verify_token.get_secret_value()
    if p.get("hub.mode") == "subscribe" and token and hmac.compare_digest(p.get("hub.verify_token", ""), token):
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


@router.post("/webhooks/meta")
async def meta_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    secret = get_settings().meta_app_secret.get_secret_value()
    if not secret or not verify_meta_signature(secret, raw, request.headers.get("x-hub-signature-256")):
        record(db, "webhook.rejected", outcome="BLOCKED", provider="META", reason="bad signature")
        db.commit()
        raise HTTPException(403, "invalid signature")
    payload = json.loads(raw or b"{}")
    handled = 0
    for ev in sorted(parse_meta_webhook(payload), key=lambda e: e.get("timestamp_ms") or 0):
        conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.kind == "customer_messaging",
                                                             IntegrationConnection.provider == META_MESSENGER.lower(),
                                                             IntegrationConnection.external_account_id == ev["page_id"]))
        if conn is None:
            continue  # Page not connected to any dealership → ignore (never guess the tenant)
        meta_ids = {k: ev[k] for k in ("page_id", "sender_id", "mid", "timestamp_ms")}
        if not record_event(db, "META", ev["mid"], meta_ids, conn.client_id):
            continue
        ts = (datetime.fromtimestamp(ev["timestamp_ms"] / 1000, timezone.utc).replace(tzinfo=None)
              if ev.get("timestamp_ms") else None)
        ingest_inbound(db, client=db.get(Client, conn.client_id), channel=META_MESSENGER,
                       external_conversation_id=ev["sender_id"], text=ev["text"], provider_message_id=ev["mid"],
                       source_timestamp=ts)
        handled += 1
    db.commit()
    return {"ok": True, "handled": handled}


def delete_contact_data(db: Session, contact: CustomerContact) -> int:
    """Remove a customer and everything recorded about them (children first: composite keys, no cascades)."""
    removed = 0
    for conv in db.scalars(select(Conversation).where(Conversation.client_id == contact.client_id,
                                                      Conversation.contact_id == contact.id)).all():
        for model in (ResponseDraft, Escalation):
            for row in db.scalars(select(model).where(model.client_id == conv.client_id, model.conversation_id == conv.id)):
                db.delete(row)
        for appt in db.scalars(select(Appointment).where(Appointment.client_id == conv.client_id,
                                                         Appointment.conversation_id == conv.id)):
            db.delete(appt)
        db.flush()
        db.delete(conv)
        removed += 1
    db.flush()
    for model in (Appointment, CustomerLead):
        for row in db.scalars(select(model).where(model.client_id == contact.client_id, model.contact_id == contact.id)):
            db.delete(row)
    db.flush()
    db.delete(contact)
    return removed


@router.post("/webhooks/meta/data-deletion")
async def meta_data_deletion(request: Request, db: Session = Depends(get_db)):
    """Meta Data Deletion Request callback (signed_request, HMAC-SHA256 with the app secret)."""
    form = await request.form()
    signed = str(form.get("signed_request", ""))
    secret = get_settings().meta_app_secret.get_secret_value()
    try:
        sig_b64, payload_b64 = signed.split(".", 1)
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        sig = base64.urlsafe_b64decode(pad(sig_b64))
        expected = hmac.new((secret or "").encode(), payload_b64.encode(), hashlib.sha256).digest()
        if not secret or not hmac.compare_digest(sig, expected):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(pad(payload_b64)))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(400, "invalid signed_request") from None
    psid = str(data.get("user_id", ""))
    code = secrets.token_hex(8)
    removed = 0
    for ident in db.scalars(select(CustomerIdentity).where(CustomerIdentity.channel == META_MESSENGER,
                                                           CustomerIdentity.external_id == psid)).all():
        removed += delete_contact_data(db, ident.contact)
    record(db, "privacy.meta_data_deletion", confirmation_code=code, conversations_removed=removed)
    db.commit()
    return {"url": f"{get_settings().base_url.rstrip('/')}/privacy/deletion/{code}", "confirmation_code": code}


@router.get("/privacy/deletion/{code}")
def deletion_status(code: str):
    return PlainTextResponse(f"Deletion request {code[:32]} has been processed.")


# --- Twilio SMS (inbound only) ----------------------------------------------------------------------------------


@router.post("/webhooks/twilio/sms")
async def twilio_webhook(request: Request, db: Session = Depends(get_db)):
    import os

    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.kind == "customer_messaging",
                                                         IntegrationConnection.provider == TWILIO_SMS.lower(),
                                                         IntegrationConnection.external_account_id == params.get("To", "")))
    if conn is None:
        raise HTTPException(404, "unknown number")
    token_env = str((conn.config or {}).get("auth_token_env", ""))
    token = os.environ.get(token_env, "") if token_env.startswith("DF_TWILIO_TOKEN_") else ""
    url = f"{get_settings().base_url.rstrip('/')}/webhooks/twilio/sms"
    if not verify_twilio_signature(token, url, params, request.headers.get("x-twilio-signature")):
        record(db, "webhook.rejected", client_id=conn.client_id, outcome="BLOCKED", provider="TWILIO", reason="bad signature")
        db.commit()
        raise HTTPException(403, "invalid signature")
    sid = params.get("MessageSid", "")
    if record_event(db, "TWILIO", sid, {k: v for k, v in params.items() if k in ("MessageSid", "From", "To")}, conn.client_id):
        ingest_inbound(db, client=db.get(Client, conn.client_id), channel=TWILIO_SMS,
                       external_conversation_id=params.get("From", ""), text=params.get("Body", ""), provider_message_id=sid)
    db.commit()
    # Empty TwiML: replies go through the outbound queue (sandbox in this build).
    return PlainTextResponse("<Response></Response>", media_type="application/xml")


# --- ADF/XML leads ----------------------------------------------------------------------------------------------


@router.post("/api/leads/adf/{connection_id}")
async def adf_intake(connection_id: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(IntegrationConnection, connection_id)
    auth = request.headers.get("authorization", "")
    expected = (conn.config or {}).get("token_sha256") if conn else None
    given = hashlib.sha256(auth.removeprefix("Bearer ").encode()).hexdigest()
    if (conn is None or conn.kind != "customer_messaging" or conn.provider != ADF_LEAD.lower() or not expected
            or not hmac.compare_digest(given, expected)):
        raise HTTPException(401, "unauthorized")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "payload too large")
    try:
        leads = parse_adf(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - malformed or hostile XML (defusedxml refuses entities/DTDs)
        raise HTTPException(400, "invalid ADF/XML") from None
    client = db.get(Client, conn.client_id)
    created = 0
    for lead in leads:
        if not record_event(db, "ADF", f"{conn.id}:{lead.lead_id}", {"lead_id": lead.lead_id}, client.id):
            continue
        vehicle = None
        if lead.vin:
            vehicle = db.scalar(select(Vehicle).where(Vehicle.client_id == client.id, Vehicle.vin == lead.vin.upper()))
        if vehicle is None and lead.stock:
            vehicle = db.scalar(select(Vehicle).where(Vehicle.client_id == client.id, Vehicle.stock_number == lead.stock))
        text = lead.comments or f"Lead from {lead.provider or 'ADF'} about {' '.join(x for x in [lead.year, lead.make, lead.model] if x)}"
        ingest_inbound(db, client=client, channel=ADF_LEAD,
                       external_conversation_id=f"adf:{lead.email or lead.phone or lead.lead_id}", text=text,
                       provider_message_id=f"{conn.id}:{lead.lead_id}", source_timestamp=lead.request_date,
                       contact_hints={"name": lead.name, "email": lead.email, "phone": lead.phone},
                       vehicle_id=vehicle.id if vehicle else None)
        created += 1
    db.commit()
    return {"ok": True, "leads": len(leads), "created": created}

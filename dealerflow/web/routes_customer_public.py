"""Public (unauthenticated) customer-messaging endpoints (Phase 3).

- Website chat: ``/chat/{key}`` test page and ``/api/chat/{key}/messages``; the key is an unguessable per-client
  secret (Automation page). Visitors only ever see replies that were really delivered (status SENT).
- Meta Messenger and Twilio SMS webhooks: signature-verified (refused when the secret is not configured), deduped
  by provider event id, routed to a client only through a matching ``customer_messaging`` connection — the tenant is
  never guessed.
- ADF/XML lead intake: per-connection bearer token (only its SHA-256 is stored), defusedxml parsing.
These endpoints only *receive*. Replies go through the send gate and are SANDBOX unless live delivery is authorized.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db, utcnow
from ..integrations import customer_channels as channels
from ..models_customer import CustomerContact, CustomerConversation, CustomerIdentity, CustomerMessage, ResponseAutomationSettings
from ..models_dealer import Client, Vehicle
from ..models_ops import IntegrationConnection
from ..services.customer import rate_limits
from ..services.customer.adf import parse_adf
from ..services.customer.processing import ingest_inbound, record_event
from .deps import render

router = APIRouter()


def meta_signature_ok(app_secret: str, raw_body: bytes, header_value: str | None) -> bool:
    """``X-Hub-Signature-256: sha256=<hex hmac>``."""
    if not app_secret or not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.removeprefix("sha256="))


def parse_meta_webhook(payload: dict) -> list[dict]:
    """Inbound Page message events (page_id, sender_id, mid, text, timestamp_ms); echoes/reads/deliveries skipped."""
    events = []
    if payload.get("object") != "page":
        return events
    for entry in payload.get("entry", []):
        page_id = str(entry.get("id", ""))
        for item in entry.get("messaging", []):
            msg = item.get("message") or {}
            if not msg or msg.get("is_echo") or "mid" not in msg:
                continue
            events.append({"page_id": page_id, "sender_id": str(item.get("sender", {}).get("id", "")), "mid": msg["mid"],
                           "text": msg.get("text") or ("[attachment]" if msg.get("attachments") else ""),
                           "timestamp_ms": item.get("timestamp")})
    return events


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def _client_for_connection(db: Session, channel: str, external_account_id: str) -> tuple[IntegrationConnection, Client] | None:
    conn = db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.kind == channels.CONNECTION_KIND, IntegrationConnection.provider == channels.PROVIDER_NAME[channel],
        IntegrationConnection.external_account_id == external_account_id, IntegrationConnection.status != "disabled"))
    if conn is None:
        return None
    client = db.get(Client, conn.client_id)
    return (conn, client) if client is not None and client.status != "offboarded" else None


# --- website chat ---------------------------------------------------------------------------------------------


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


def _client_by_chat_key(db: Session, key: str) -> Client:
    s = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.chat_widget_key == key)) if len(key) >= 16 else None
    client = db.get(Client, s.client_id) if s else None
    if client is None or client.status == "offboarded":
        raise HTTPException(404, "Not found")
    return client


@router.options("/api/chat/{key}/messages")
def chat_preflight(key: str, request: Request, db: Session = Depends(get_db)):
    return _cors(request, _client_by_chat_key(db, key), PlainTextResponse(""))


@router.post("/api/chat/{key}/messages")
async def chat_post(key: str, request: Request, db: Session = Depends(get_db)):
    client = _client_by_chat_key(db, key)
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
    per_hour = get_settings().chat_max_messages_per_visitor_per_hour
    if not (rate_limits.try_acquire(db, f"chat:ip:{client.id}:{ip}", 30, 60)
            and rate_limits.try_acquire(db, f"chat:visitor:{client.id}:{visitor}", per_hour, 3600)):
        db.commit()
        return _cors(request, client, JSONResponse({"error": "Too many messages. Please wait a moment."}, 429))
    client_msg_id = str(data.get("client_message_id") or uuid.uuid4())[:64]
    vehicle_id = None
    stock = str(data.get("vehicle_stock") or "")[:50]
    if stock:
        v = db.scalar(select(Vehicle).where(Vehicle.client_id == client.id, Vehicle.stock_number == stock))
        vehicle_id = v.id if v else None
    res = ingest_inbound(db, dealership=client, channel=channels.WEBSITE_CHAT, external_conversation_id=f"web:{visitor}", text=text,
                         provider_message_id=f"{client.id}:{visitor}:{client_msg_id}",
                         contact_hints={"name": str(data.get("name") or "")[:100] or None}, vehicle_id=vehicle_id)
    if data.get("sms_consent") is True and data.get("phone"):
        contact = db.get(CustomerContact, res.conversation.contact_id)
        contact.phone = str(data["phone"])[:40]
        contact.sms_consent = True
        contact.sms_consent_source = f"website chat checkbox {utcnow().isoformat()} ip={ip}: {str(data.get('consent_text', ''))[:300]}"
    db.commit()
    return _cors(request, client, JSONResponse({"ok": True, "message_id": res.message.id, "duplicate": not res.created}))


@router.get("/api/chat/{key}/messages")
def chat_poll(key: str, visitor_id: str, request: Request, after_id: int = 0, db: Session = Depends(get_db)):
    client = _client_by_chat_key(db, key)
    conv = db.scalar(select(CustomerConversation).where(CustomerConversation.client_id == client.id,
                                                        CustomerConversation.channel == channels.WEBSITE_CHAT,
                                                        CustomerConversation.external_conversation_id == f"web:{visitor_id[:64]}"))
    out = []
    if conv:
        for m in db.scalars(select(CustomerMessage).where(CustomerMessage.conversation_id == conv.id, CustomerMessage.id > after_id,
                                                          CustomerMessage.direction == "OUTBOUND", CustomerMessage.delivery_status == "SENT")
                            .order_by(CustomerMessage.id)):
            out.append({"id": m.id, "text": m.body, "at": m.created_at.isoformat()})
    return _cors(request, client, JSONResponse({"messages": out}))


@router.get("/chat/{key}")
def chat_test_page(key: str, request: Request, db: Session = Depends(get_db)):
    client = _client_by_chat_key(db, key)
    return render(request, "chat_page.html", None, client=client, chat_key=key)


# --- Meta Messenger -------------------------------------------------------------------------------------------


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
    if len(raw) > 1_000_000:
        raise HTTPException(413, "payload too large")
    secret = get_settings().meta_app_secret.get_secret_value()
    if not meta_signature_ok(secret, raw, request.headers.get("x-hub-signature-256")):
        audit.event(db, "webhook.rejected", provider="meta", reason="missing secret or bad signature")
        db.commit()
        raise HTTPException(403, "invalid signature")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON") from None
    handled = 0
    for ev in sorted(parse_meta_webhook(payload), key=lambda e: e.get("timestamp_ms") or 0):
        found = _client_for_connection(db, channels.META_MESSENGER, ev["page_id"])
        if found is None:
            continue  # page not connected to any dealership: ignore (never guess the tenant)
        _conn, client = found
        ids = {k: ev[k] for k in ("page_id", "sender_id", "mid", "timestamp_ms")}  # identifiers only; text lives on the message
        if not record_event(db, "META", ev["mid"], ids, client.id):
            continue
        ts = datetime.fromtimestamp(ev["timestamp_ms"] / 1000, UTC).replace(tzinfo=None) if ev.get("timestamp_ms") else None
        ingest_inbound(db, dealership=client, channel=channels.META_MESSENGER, external_conversation_id=ev["sender_id"],
                       text=ev["text"], provider_message_id=ev["mid"], source_timestamp=ts)
        handled += 1
    db.commit()
    return {"ok": True, "handled": handled}


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
    for ident in list(db.scalars(select(CustomerIdentity).where(CustomerIdentity.channel == channels.META_MESSENGER,
                                                                CustomerIdentity.external_id == psid))):
        contact = db.get(CustomerContact, ident.contact_id)
        for conv in db.scalars(select(CustomerConversation).where(CustomerConversation.client_id == ident.client_id,
                                                                  CustomerConversation.contact_id == ident.contact_id)):
            db.delete(conv)
        db.flush()
        if contact is not None:
            db.delete(contact)
        removed += 1
    audit.event(db, "privacy.meta_data_deletion", confirmation_code=code, contacts_removed=removed)
    db.commit()
    return {"url": f"{get_settings().base_url.rstrip('/')}/privacy/deletion/{code}", "confirmation_code": code}


@router.get("/privacy/deletion/{code}")
def deletion_status(code: str):
    return PlainTextResponse(f"Deletion request {code[:32]} has been processed.")


# --- Twilio SMS -----------------------------------------------------------------------------------------------


@router.post("/webhooks/twilio/sms")
async def twilio_sms_webhook(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    token = get_settings().twilio_auth_token.get_secret_value()
    url = f"{get_settings().base_url.rstrip('/')}/webhooks/twilio/sms"
    sent = request.headers.get("x-twilio-signature")
    if not token or not sent or not hmac.compare_digest(twilio_signature(token, url, params), sent):
        audit.event(db, "webhook.rejected", provider="twilio_sms", reason="missing token or bad signature")
        db.commit()
        raise HTTPException(403, "invalid signature")
    found = _client_for_connection(db, channels.TWILIO_SMS, params.get("To", ""))
    if found is None:
        raise HTTPException(404, "unknown number")
    _conn, client = found
    sid = params.get("MessageSid", "")
    if sid and record_event(db, "TWILIO", sid, {k: v for k, v in params.items() if k in ("MessageSid", "From", "To")}, client.id):
        ingest_inbound(db, dealership=client, channel=channels.TWILIO_SMS, external_conversation_id=params.get("From", ""),
                       text=params.get("Body", ""), provider_message_id=sid)
    db.commit()
    return PlainTextResponse("<Response></Response>", media_type="application/xml")  # replies go through the send queue


# --- ADF/XML leads --------------------------------------------------------------------------------------------


@router.post("/api/leads/adf/{connection_id}")
async def adf_intake(connection_id: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(IntegrationConnection, connection_id)
    expected = (conn.config or {}).get("token_sha256") if conn else None
    given = hashlib.sha256(request.headers.get("authorization", "").removeprefix("Bearer ").encode()).hexdigest()
    if (conn is None or conn.kind != channels.CONNECTION_KIND or conn.provider != channels.PROVIDER_NAME[channels.ADF_LEAD]
            or conn.status == "disabled" or not expected or not hmac.compare_digest(given, expected)):
        raise HTTPException(401, "unauthorized")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "payload too large")
    try:
        leads = parse_adf(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - malformed or hostile XML
        raise HTTPException(400, "invalid ADF/XML") from None
    client = db.get(Client, conn.client_id)
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
        ingest_inbound(db, dealership=client, channel=channels.ADF_LEAD, external_conversation_id=f"adf:{lead.email or lead.phone or lead.lead_id}",
                       text=text, provider_message_id=f"{conn.id}:{lead.lead_id}", source_timestamp=lead.request_date,
                       contact_hints={"name": lead.name, "email": lead.email, "phone": lead.phone}, vehicle_id=vehicle.id if vehicle else None)
        created += 1
    db.commit()
    return {"ok": True, "leads": len(leads), "created": created}

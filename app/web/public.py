"""Unauthenticated endpoints: website chat API, provider webhooks, ADF intake, booking, unsubscribe."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.integrations import meta_messenger, registry, twilio_sms
from app.models import (
    Conversation,
    CustomerContact,
    CustomerIdentity,
    Dealership,
    IntegrationConnection,
    Message,
    OutreachMessage,
    Prospect,
    SalesMeeting,
    Vehicle,
)
from app.security.crypto import CredentialEncryptionError, decrypt_json
from app.services import audit
from app.services.inbox.adf import parse_adf
from app.services.inbox.processing import ingest_inbound, record_event
from app.services.outreach.agent import get_state
from app.services.outreach.booking import available_slots, book
from app.services.outreach.compliance import add_suppression
from app.web.deps import RateLimiter, render

router = APIRouter()
_chat_ip_limiter = RateLimiter(30, 60)
_chat_visitor_limiter = RateLimiter(8, 60)


# ----------------------------------------------------------------------------- website chat


def _cors(request: Request, dealership: Dealership, resp):
    origin = request.headers.get("origin")
    allowed = {get_settings().base_url.rstrip("/")}
    if dealership.website:
        allowed.add(dealership.website.rstrip("/"))
    if origin and origin.rstrip("/") in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _dealer_by_key(db: Session, key: str) -> Dealership:
    d = db.scalar(select(Dealership).where(Dealership.public_chat_key == key))
    if d is None or d.status == "SUSPENDED":
        raise HTTPException(404, "Not found")
    return d


@router.options("/api/chat/{key}/messages")
def chat_preflight(key: str, request: Request, db: Session = Depends(get_db)):
    return _cors(request, _dealer_by_key(db, key), PlainTextResponse(""))


@router.post("/api/chat/{key}/messages")
async def chat_post(key: str, request: Request, db: Session = Depends(get_db)):
    dealership = _dealer_by_key(db, key)
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON") from None
    visitor = str(data.get("visitor_id", ""))[:64]
    text = str(data.get("text", "")).strip()[:2000]
    if not visitor or len(visitor) < 8 or not text:
        raise HTTPException(400, "visitor_id and text are required")
    ip = request.client.host if request.client else "?"
    if not _chat_ip_limiter.allow(f"{key}:{ip}") or not _chat_visitor_limiter.allow(f"{key}:{visitor}"):
        return _cors(request, dealership, JSONResponse({"error": "Too many messages. Please wait a moment."}, 429))
    client_msg_id = str(data.get("client_message_id") or uuid.uuid4())[:64]
    vehicle_id = None
    stock = str(data.get("vehicle_stock") or "")[:50]
    if stock:
        v = db.scalar(select(Vehicle).where(Vehicle.dealership_id == dealership.id, Vehicle.stock_number == stock))
        vehicle_id = v.id if v else None
    hints = {"name": str(data.get("name") or "")[:100] or None}
    res = ingest_inbound(db, dealership=dealership, channel=registry.WEBSITE_CHAT, external_conversation_id=f"web:{visitor}",
                         text=text, provider_message_id=f"{visitor}:{client_msg_id}", contact_hints=hints,
                         vehicle_id=vehicle_id)
    if data.get("sms_consent") is True and data.get("phone"):
        contact = db.get(CustomerContact, res.conversation.contact_id)
        contact.phone = str(data["phone"])[:40]
        contact.sms_consent = True
        contact.sms_consent_source = (f"website chat checkbox {utcnow().isoformat()} ip={ip}: "
                                      f"{str(data.get('consent_text', ''))[:300]}")
    db.commit()
    return _cors(request, dealership, JSONResponse({"ok": True, "message_id": res.message.id, "duplicate": not res.created}))


@router.get("/api/chat/{key}/messages")
def chat_poll(key: str, visitor_id: str, request: Request, after_id: int = 0, db: Session = Depends(get_db)):
    dealership = _dealer_by_key(db, key)
    conv = db.scalar(select(Conversation).where(Conversation.dealership_id == dealership.id,
                                                Conversation.channel == registry.WEBSITE_CHAT,
                                                Conversation.external_conversation_id == f"web:{visitor_id[:64]}"))
    out = []
    if conv:
        for m in db.scalars(select(Message).where(Message.conversation_id == conv.id, Message.id > after_id,
                                                  Message.direction == "OUTBOUND", Message.delivery_status == "SENT")
                            .order_by(Message.id)):
            out.append({"id": m.id, "text": m.body, "at": m.created_at.isoformat()})
    return _cors(request, dealership, JSONResponse({"messages": out}))


@router.get("/chat/{key}")
def chat_demo_page(key: str, request: Request, db: Session = Depends(get_db)):
    dealership = _dealer_by_key(db, key)
    return render(request, "chat_page.html", dealership=dealership)


# ----------------------------------------------------------------------------- Meta Messenger


@router.get("/webhooks/meta")
def meta_verify(request: Request):
    p = request.query_params
    token = get_settings().meta_verify_token
    if p.get("hub.mode") == "subscribe" and token and hmac.compare_digest(p.get("hub.verify_token", ""), token):
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


@router.post("/webhooks/meta")
async def meta_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    secret = get_settings().meta_app_secret
    if not secret or not meta_messenger.verify_signature(secret, raw, request.headers.get("x-hub-signature-256")):
        audit.record(db, "webhook.rejected", actor_type="WEBHOOK", outcome="BLOCKED", provider="META", reason="bad signature")
        db.commit()
        raise HTTPException(403, "invalid signature")
    payload = json.loads(raw or b"{}")
    handled = 0
    for ev in sorted(meta_messenger.parse_webhook(payload), key=lambda e: e.get("timestamp_ms") or 0):
        conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == registry.META_MESSENGER,
                                                             IntegrationConnection.external_account_id == ev["page_id"]))
        if conn is None or conn.dealership_id is None:
            continue  # page not connected to any dealership → ignore (never guess the tenant)
        if not record_event(db, "META", ev["mid"], ev, conn.dealership_id):
            continue
        ts = datetime.fromtimestamp(ev["timestamp_ms"] / 1000, UTC).replace(tzinfo=None) if ev.get("timestamp_ms") else None
        ingest_inbound(db, dealership=db.get(Dealership, conn.dealership_id), channel=registry.META_MESSENGER,
                       external_conversation_id=ev["sender_id"], text=ev["text"], provider_message_id=ev["mid"],
                       source_timestamp=ts)
        handled += 1
    db.commit()
    return {"ok": True, "handled": handled}


@router.post("/webhooks/meta/data-deletion")
async def meta_data_deletion(request: Request, db: Session = Depends(get_db)):
    """Meta Data Deletion Request callback (signed_request, HMAC-SHA256 with app secret)."""
    form = await request.form()
    signed = str(form.get("signed_request", ""))
    secret = get_settings().meta_app_secret
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
    for ident in db.scalars(select(CustomerIdentity).where(CustomerIdentity.channel == registry.META_MESSENGER,
                                                           CustomerIdentity.external_id == psid)):
        for conv in db.scalars(select(Conversation).where(Conversation.contact_id == ident.contact_id)):
            db.delete(conv)
        db.delete(ident.contact)
        removed += 1
    audit.record(db, "privacy.meta_data_deletion", actor_type="WEBHOOK", confirmation_code=code, contacts_removed=removed)
    db.commit()
    return {"url": f"{get_settings().base_url.rstrip('/')}/privacy/deletion/{code}", "confirmation_code": code}


@router.get("/privacy/deletion/{code}")
def deletion_status(code: str):
    return PlainTextResponse(f"Deletion request {code} has been processed.")


# ----------------------------------------------------------------------------- Twilio SMS


@router.post("/webhooks/twilio/sms")
async def twilio_webhook(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == registry.TWILIO_SMS,
                                                         IntegrationConnection.external_account_id == params.get("To", "")))
    if conn is None or conn.dealership_id is None:
        raise HTTPException(404, "unknown number")
    try:
        token = decrypt_json(conn.encrypted_credentials).get("auth_token", "")
    except CredentialEncryptionError:
        token = ""
    url = f"{get_settings().base_url.rstrip('/')}/webhooks/twilio/sms"
    if not twilio_sms.validate_signature(token, url, params, request.headers.get("x-twilio-signature")):
        audit.record(db, "webhook.rejected", actor_type="WEBHOOK", outcome="BLOCKED", provider="TWILIO",
                     dealership_id=conn.dealership_id, reason="bad signature")
        db.commit()
        raise HTTPException(403, "invalid signature")
    sid = params.get("MessageSid", "")
    if record_event(db, "TWILIO", sid, {k: v for k, v in params.items() if k in ("MessageSid", "From", "To")},
                    conn.dealership_id):
        ingest_inbound(db, dealership=db.get(Dealership, conn.dealership_id), channel=registry.TWILIO_SMS,
                       external_conversation_id=params.get("From", ""), text=params.get("Body", ""),
                       provider_message_id=sid)
    db.commit()
    # Empty TwiML: replies are sent asynchronously through the outbound queue.
    return PlainTextResponse("<Response></Response>", media_type="application/xml")


# ----------------------------------------------------------------------------- ADF/XML leads


@router.post("/api/leads/adf/{connection_id}")
async def adf_intake(connection_id: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(IntegrationConnection, connection_id)
    auth = request.headers.get("authorization", "")
    expected = (conn.config or {}).get("token_sha256") if conn else None
    given = hashlib.sha256(auth.removeprefix("Bearer ").encode()).hexdigest()
    if conn is None or conn.provider != registry.ADF_LEAD or not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(401, "unauthorized")
    raw = (await request.body()).decode("utf-8", "replace")
    try:
        leads = parse_adf(raw)
    except Exception:  # noqa: BLE001 - malformed/hostile XML
        raise HTTPException(400, "invalid ADF/XML") from None
    dealership = db.get(Dealership, conn.dealership_id)
    created = 0
    for lead in leads:
        if not record_event(db, "ADF", f"{conn.id}:{lead.lead_id}", {"lead_id": lead.lead_id}, dealership.id):
            continue
        vehicle = None
        if lead.vin:
            vehicle = db.scalar(select(Vehicle).where(Vehicle.dealership_id == dealership.id, Vehicle.vin == lead.vin))
        if vehicle is None and lead.stock:
            vehicle = db.scalar(select(Vehicle).where(Vehicle.dealership_id == dealership.id,
                                                      Vehicle.stock_number == lead.stock))
        text = lead.comments or f"Lead from {lead.provider or 'ADF'} about {' '.join(x for x in [lead.year, lead.make, lead.model] if x)}"
        ingest_inbound(db, dealership=dealership, channel=registry.ADF_LEAD,
                       external_conversation_id=f"adf:{lead.email or lead.phone or lead.lead_id}", text=text,
                       provider_message_id=f"{conn.id}:{lead.lead_id}", source_timestamp=lead.request_date,
                       contact_hints={"name": lead.name, "email": lead.email, "phone": lead.phone},
                       vehicle_id=vehicle.id if vehicle else None)
        created += 1
    db.commit()
    return {"ok": True, "leads": len(leads), "created": created}


# ----------------------------------------------------------------------------- outreach: unsubscribe + booking


@router.get("/u/{token}")
def unsubscribe_page(token: str, request: Request, db: Session = Depends(get_db)):
    msg = db.scalar(select(OutreachMessage).where(OutreachMessage.unsubscribe_token == token))
    if msg is None:
        raise HTTPException(404, "Not found")
    return render(request, "unsubscribe.html", token=token, done=False)


@router.post("/u/{token}")
def unsubscribe(token: str, request: Request, db: Session = Depends(get_db)):
    """Handles both the confirmation form and RFC 8058 one-click POSTs."""
    msg = db.scalar(select(OutreachMessage).where(OutreachMessage.unsubscribe_token == token))
    if msg is None:
        raise HTTPException(404, "Not found")
    prospect = db.get(Prospect, msg.prospect_id)
    add_suppression(db, "EMAIL", msg.to_address, "OPT_OUT", f"unsubscribe link message #{msg.id}")
    from app.services.outreach.agent import _stop_enrollments

    _stop_enrollments(db, prospect, "opted_out")
    prospect.status, prospect.status_reason = "DO_NOT_CONTACT", "unsubscribed via link"
    audit.record(db, "outreach.unsubscribed", actor_type="CUSTOMER", entity_type="prospect", entity_id=prospect.id)
    db.commit()
    return render(request, "unsubscribe.html", token=token, done=True)


@router.get("/book/{token}")
def booking_page(token: str, request: Request, db: Session = Depends(get_db)):
    meeting = db.scalar(select(SalesMeeting).where(SalesMeeting.booking_token == token))
    if meeting is None:
        raise HTTPException(404, "Not found")
    st = get_state(db)
    slots = available_slots(db, st) if meeting.status == "PROPOSED" else []
    return render(request, "booking.html", meeting=meeting, slots=slots[:40], tz=st.timezone)


@router.post("/book/{token}")
def booking_submit(token: str, request: Request, slot: str = Form(...), db: Session = Depends(get_db)):
    meeting = db.scalar(select(SalesMeeting).where(SalesMeeting.booking_token == token))
    if meeting is None:
        raise HTTPException(404, "Not found")
    st = get_state(db)
    try:
        start = datetime.fromisoformat(slot)
        if start not in available_slots(db, st):
            raise ValueError("That time is no longer available. Please pick another time.")
        book(db, meeting, start, st)
        db.commit()
    except ValueError as exc:
        db.commit()  # keep audit of failures
        return render(request, "booking.html", meeting=meeting, slots=available_slots(db, st)[:40], tz=st.timezone,
                      error=str(exc))
    return render(request, "booking.html", meeting=meeting, slots=[], tz=st.timezone)

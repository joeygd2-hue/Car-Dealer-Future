"""Unauthenticated endpoints: health, unsubscribe and provider webhooks (all verified)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..db import get_db
from ..integrations.zoom import url_validation_response, verify_webhook
from ..models import Call, Meeting
from ..security import unsign
from ..services import agent
from ..services.crm import add_activity
from ..services.outreach import handle_unsubscribe
from .deps import templates

router = APIRouter()


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    h = agent.health(db)
    return JSONResponse({"ok": True, "db": "ok", **h})


def _page(request: Request, title: str, body: str):
    return templates.TemplateResponse(request, "public.html", {"title": title, "body": body})


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str, request: Request):
    if not unsign(token, "unsubscribe"):
        return _page(request, "Link not valid", "This unsubscribe link is not valid. Reply to the email with "
                     "'unsubscribe' and we will remove you.")
    return templates.TemplateResponse(request, "unsubscribe.html", {"token": token})


@router.post("/u/{token}")
async def unsubscribe(token: str, request: Request, db: Session = Depends(get_db)):
    """Handles both the confirmation form and RFC 8058 one-click POSTs from mail clients."""
    value = unsign(token, "unsubscribe")
    if not value:
        raise HTTPException(400, "invalid unsubscribe link")
    email = handle_unsubscribe(db, value, source="unsubscribe link")
    return _page(request, "You're unsubscribed",
                 f"{email} will not receive further emails from us. / {email} no recibirá más correos de nuestra parte.")


@router.post("/webhooks/zoom")
async def zoom_webhook(request: Request, db: Session = Depends(get_db)):
    secret = get_settings().zoom_webhook_secret_token.get_secret_value()
    if not secret:
        raise HTTPException(404, "Zoom webhooks not configured")
    body = await request.body()
    if not verify_webhook(body, request.headers.get("x-zm-request-timestamp", ""),
                          request.headers.get("x-zm-signature", ""), secret):
        raise HTTPException(401, "bad signature")
    data = json.loads(body)
    event = data.get("event")
    if event == "endpoint.url_validation":
        return url_validation_response(data["payload"]["plainToken"], secret)
    obj = data.get("payload", {}).get("object", {})
    m = db.scalar(select(Meeting).where(Meeting.zoom_meeting_id == str(obj.get("id"))))
    if m:
        if event == "meeting.ended" and m.status in ("booked", "rescheduled"):
            m.status = "completed"
            add_activity(db, m.prospect, "meeting", "Zoom reports the meeting ended", ref_type="meeting", ref_id=m.id)
        elif event == "meeting.deleted" and m.status in ("booked", "rescheduled"):
            m.status = "cancelled"
            add_activity(db, m.prospect, "meeting", "Zoom meeting was deleted in Zoom", ref_type="meeting", ref_id=m.id)
        audit(db, "webhook.zoom", entity_type="meeting", entity_id=m.id, data={"event": event})
    return {"ok": True}


def _twilio_valid(request: Request, params: dict) -> bool:
    token = get_settings().twilio_auth_token.get_secret_value()
    if not token:
        return False
    url = f"{get_settings().base_url}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
    data = url + "".join(k + params[k] for k in sorted(params))
    expected = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, request.headers.get("x-twilio-signature", ""))


@router.post("/webhooks/twilio/status")
async def twilio_status(request: Request, call_id: int, db: Session = Depends(get_db)):
    form = dict(await request.form())
    if not _twilio_valid(request, {k: str(v) for k, v in form.items()}):
        raise HTTPException(401, "bad signature")
    call = db.get(Call, call_id)
    if call:
        call.status = str(form.get("CallStatus", call.status))
        if form.get("CallDuration"):
            call.duration_seconds = int(form["CallDuration"])
    return {"ok": True}


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe snapshot events (signature-verified, de-duplicated by event id, processed by a job)."""
    from ..integrations.stripe import SignatureError, ingest

    raw = await request.body()
    if len(raw) > 1_000_000:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    try:
        status, _ = ingest(db, raw, request.headers.get("stripe-signature"))
    except (SignatureError, json.JSONDecodeError):
        return JSONResponse({"detail": "Invalid signature or payload"}, status_code=400)
    return {"status": status}

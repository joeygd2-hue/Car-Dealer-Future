"""Unauthenticated endpoints: unsubscribe and signed webhooks."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations import inbox
from app.integrations.stripe_webhooks import SignatureError, ingest
from app.services.agent import messaging
from app.web.common import TEMPLATES

router = APIRouter()

MAX_WEBHOOK_BYTES = 1_000_000


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str, request: Request):
    return TEMPLATES.TemplateResponse(request, "unsubscribe.html", {"token": token, "done": False, "user": None})


@router.post("/unsubscribe/{token}")
def unsubscribe(token: str, request: Request, db: Session = Depends(get_db)):
    """Handles both the confirmation form and RFC 8058 one-click POSTs. Always responds the same way."""
    if len(token) <= 64:
        messaging.unsubscribe(db, token, source="post")
        db.commit()
    if "text/html" in request.headers.get("accept", ""):
        return TEMPLATES.TemplateResponse(request, "unsubscribe.html", {"token": token, "done": True, "user": None})
    return JSONResponse({"status": "unsubscribed"})


@router.post("/api/v1/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    try:
        status, _ = ingest(db, raw, request.headers.get("stripe-signature"))
    except (SignatureError, json.JSONDecodeError):
        return JSONResponse({"detail": "Invalid signature or payload"}, status_code=400)
    return {"status": status}


@router.post("/api/v1/webhooks/inbound-email")
async def inbound_email_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    if not inbox.verify_webhook_signature(raw, request.headers.get("x-signature")):
        return JSONResponse({"detail": "Invalid signature"}, status_code=401)
    try:
        data = json.loads(raw)
        _, new = messaging.ingest_inbound(
            db, provider="webhook", provider_message_id=str(data["message_id"]), from_address=str(data["from"]), subject=data.get("subject"),
            body=str(data.get("text", "")), in_reply_to=data.get("in_reply_to"), to_addresses=list(data.get("to", [])), headers=dict(data.get("headers", {})),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return JSONResponse({"detail": "Invalid payload"}, status_code=400)
    db.commit()
    return {"status": "accepted" if new else "duplicate"}

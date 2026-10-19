"""Unauthenticated Phase 3 endpoints: website chat / inquiry API, demo chat page, ADF/XML lead intake.

No session or cookie is involved (so no CSRF). Protection: a per-dealership key, database-backed rate limits shared
by every process, size limits, CORS limited to the dealership's own website, and a widget that only ever shows
replies that were really delivered (``sent``) — SIMULATED replies stay inside the platform.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Client, Conversation, ConversationMessage
from ..models_conversations import WEBSITE_CHAT
from ..services import rate_limits
from ..services.conversations.public import client_by_key, submit_adf, submit_chat
from .deps import render

router = APIRouter()

MAX_ADF_BYTES = 256 * 1024


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _cors(request: Request, client: Client, resp):
    origin = (request.headers.get("origin") or "").rstrip("/")
    allowed = {get_settings().base_url.rstrip("/")}
    if client.website:
        allowed.add(client.website.rstrip("/"))
    if origin and origin in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _chat_client(db: Session, key: str) -> Client:
    c = client_by_key(db, chat_key=key)
    if c is None:
        raise HTTPException(404, "Not found")
    return c


@router.options("/api/public/chat/{key}/messages")
def chat_preflight(key: str, request: Request, db: Session = Depends(get_db)):
    return _cors(request, _chat_client(db, key), PlainTextResponse(""))


@router.post("/api/public/chat/{key}/messages")
async def chat_post(key: str, request: Request, db: Session = Depends(get_db)):
    client = _chat_client(db, key)
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "invalid JSON")
    s = get_settings()
    visitor = str(data.get("visitor_id", ""))[:64]
    if not (rate_limits.try_acquire(db, f"chat:ip:{client.id}:{_client_ip(request)}", s.chat_rate_limit_per_minute, 60)
            and rate_limits.try_acquire(db, f"chat:visitor:{client.id}:{visitor}", 8, 60)):
        db.commit()
        return _cors(request, client, JSONResponse({"error": "Too many messages. Please wait a moment."}, 429))
    try:
        res = submit_chat(db, client, visitor=visitor, text=str(data.get("text", "")),
                          client_message_id=str(data.get("client_message_id") or "") or None,
                          hints={"name": data.get("name"), "email": data.get("email"), "phone": data.get("phone")},
                          vehicle_stock=str(data.get("vehicle_stock") or "") or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    db.commit()
    return _cors(request, client, JSONResponse({"ok": True, "message_id": res.message.id, "duplicate": not res.created}))


@router.get("/api/public/chat/{key}/messages")
def chat_poll(key: str, visitor_id: str, request: Request, after_id: int = 0, db: Session = Depends(get_db)):
    client = _chat_client(db, key)
    if not rate_limits.try_acquire(db, f"chatpoll:ip:{client.id}:{_client_ip(request)}", 120, 60):
        db.commit()
        return _cors(request, client, JSONResponse({"error": "Too many requests"}, 429))
    db.commit()
    conv = db.scalar(select(Conversation).where(Conversation.client_id == client.id, Conversation.channel == WEBSITE_CHAT,
                                                Conversation.external_conversation_id == f"web:{visitor_id[:64]}"))
    out = []
    if conv:
        for m in db.scalars(select(ConversationMessage).where(
                ConversationMessage.client_id == client.id, ConversationMessage.conversation_id == conv.id,
                ConversationMessage.id > after_id, ConversationMessage.direction == "outbound",
                ConversationMessage.delivery_status == "sent").order_by(ConversationMessage.id)):
            out.append({"id": m.id, "text": m.body, "at": m.created_at.isoformat()})
    return _cors(request, client, JSONResponse({"messages": out}))


@router.get("/chat/{key}")
def chat_page(key: str, request: Request, db: Session = Depends(get_db)):
    """Minimal hosted chat page / reference widget for the dealership's website."""
    return render(request, "chat_page.html", None, client=_chat_client(db, key), key=key)


@router.post("/api/public/leads/adf/{key}")
async def adf_intake(key: str, request: Request, db: Session = Depends(get_db)):
    client = client_by_key(db, adf_key=key)
    if client is None:
        raise HTTPException(404, "Not found")
    if not rate_limits.try_acquire(db, f"adf:{client.id}", 120, 60):
        db.commit()
        return JSONResponse({"error": "rate limited"}, 429)
    body = await request.body()
    if len(body) > MAX_ADF_BYTES:
        raise HTTPException(413, "ADF document too large")
    try:
        results = submit_adf(db, client, body.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - malformed or hostile XML (defusedxml refuses entities/DTDs)
        raise HTTPException(400, "Invalid ADF/XML document") from None
    db.commit()
    return {"ok": True, "received": len(results), "duplicates": sum(1 for r in results if not r.created)}

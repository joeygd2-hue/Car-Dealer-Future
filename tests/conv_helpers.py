"""Shared fixtures for the Phase 3 (customer conversations) tests. Synthetic data only; no network."""

from __future__ import annotations

from sqlalchemy import select

from dealerflow.db import session_scope, utcnow
from dealerflow.models import Conversation, ConversationMessage, ResponseDraft, User
from dealerflow.models_dealer import Client
from dealerflow.security import hash_password, new_api_key
from tests.dealer_helpers import PASSWORD, SAMPLE_CSV, import_csv, make_client

ALL_WEEK = {str(d): ["00:00", "23:59"] for d in range(7)}  # "always open", so business-hours rules never interfere
SHOP_HOURS = {**{str(d): ["09:00", "19:00"] for d in range(6)}, "6": None}


def user_with_key(email: str, *, role: str = "viewer", client_id: int | None = None, client_role: str | None = None) -> dict:
    key, digest = new_api_key()
    with session_scope() as db:
        db.add(User(email=email, name=email.split("@")[0], role=role, client_id=client_id, client_role=client_role,
                    password_hash=hash_password(PASSWORD), api_key_hash=digest))
    return {"Authorization": f"Bearer {key}"}


def setup_dealer(name: str = "Alpha Auto", *, hours: dict | None = None, approved: bool = True,
                 contact_email: str | None = None) -> dict:
    cid = make_client(name)
    import_csv(cid)[0].close()  # Phase 2 import: A100 2003 Honda Accord, B200 2019 Toyota Camry, C300 2018 Ford F-150
    slug = name.lower().replace(" ", "-")
    with session_scope() as db:
        c = db.get(Client, cid)
        c.phone, c.address_line, c.city, c.state, c.postal_code = "(281) 555-0100", "123 Main St", "Sugar Land", "TX", "77478"
        c.business_hours = hours or ALL_WEEK
        c.business_info_approved_at = utcnow() if approved else None
        c.public_chat_key, c.adf_intake_key = f"chat-{slug}-key-0001", f"adf-{slug}-secret-0001"
        c.contact_email = contact_email or f"owner@{slug}.example"
    h = {
        "admin": user_with_key(f"admin@{slug}.example", client_id=cid, client_role="dealer_admin"),
        "reviewer": user_with_key(f"reviewer@{slug}.example", client_id=cid, client_role="dealer_reviewer"),
        "viewer": user_with_key(f"viewer@{slug}.example", client_id=cid, client_role="dealer_viewer"),
    }
    return {"id": cid, "key": f"chat-{slug}-key-0001", "adf": f"adf-{slug}-secret-0001", "h": h, "slug": slug}


def chat(http, key: str, text: str, *, visitor: str = "visitor-0001", msg_id: str | None = None, **hints):
    payload = {"visitor_id": visitor, "text": text, "client_message_id": msg_id, **hints}
    return http.post(f"/api/public/chat/{key}/messages", json=payload)


def only_conversation(client_id: int) -> Conversation:
    with session_scope() as db:
        rows = list(db.scalars(select(Conversation).where(Conversation.client_id == client_id)))
        assert len(rows) == 1, rows
        db.expunge(rows[0])
        return rows[0]


def pending_draft(conversation_id: int) -> ResponseDraft:
    with session_scope() as db:
        d = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conversation_id,
                                                  ResponseDraft.status == "pending_review"))
        assert d is not None
        db.expunge(d)
        return d


def outbound(conversation_id: int) -> list[ConversationMessage]:
    with session_scope() as db:
        rows = list(db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id,
                                                                 ConversationMessage.direction == "outbound")
                               .order_by(ConversationMessage.id)))
        for r in rows:
            db.expunge(r)
        return rows


__all__ = ["SAMPLE_CSV", "ALL_WEEK", "SHOP_HOURS", "setup_dealer", "chat", "user_with_key", "only_conversation",
           "pending_draft", "outbound"]

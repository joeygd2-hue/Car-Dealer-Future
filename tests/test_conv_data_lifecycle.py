"""Customer data lifecycle: retention purge, export and confirmed deletion of a dealership's conversations."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select, update

from dealerflow.db import session_scope, utcnow
from dealerflow.models import (
    Conversation,
    ConversationMessage,
    CustomerContact,
    CustomerIdentity,
    CustomerSuppression,
    ResponseDraft,
    User,
)
from dealerflow.models_dealer import Client, Vehicle
from dealerflow.services.maintenance import delete_client, export_client, purge_conversation_pii
from tests.conv_helpers import chat, only_conversation, setup_dealer
from tests.dealer_helpers import drain


def test_retention_purge_removes_customer_text_but_keeps_opt_outs(client):
    d = setup_dealer()
    chat(client, d["key"], "Is the Accord available? I'm Jo, jo@buyer.example", email="jo@buyer.example", name="Jo")
    chat(client, d["key"], "STOP", msg_id="stop")
    drain()
    conv = only_conversation(d["id"])
    with session_scope() as db:
        db.execute(update(Conversation).values(created_at=utcnow() - timedelta(days=400), last_inbound_at=utcnow() - timedelta(days=400)))
    with session_scope() as db:
        assert purge_conversation_pii(db, 365) == 1
    with session_scope() as db:
        assert {m.body for m in db.scalars(select(ConversationMessage))} == {"[purged]"}
        assert {x.body for x in db.scalars(select(ResponseDraft))} <= {"[purged]"}
        c = db.get(CustomerContact, conv.contact_id)
        assert c.email is None and c.display_name is None and c.pii_purged_at is not None
        assert db.scalar(select(func.count()).select_from(CustomerIdentity)) == 0  # visitor ids / ADF emails removed
        assert db.scalar(select(func.count()).select_from(CustomerSuppression)) == 1  # opt-out still honoured
        assert db.get(Conversation, conv.id).pii_purged_at is not None
        assert purge_conversation_pii(db, 365) == 0  # idempotent


def test_export_and_delete_a_dealership_with_conversations(client, db, admin, tmp_path):
    a, b = setup_dealer("Alpha Auto"), setup_dealer("Bravo Cars")
    chat(client, a["key"], "Can I test drive the Accord tomorrow at 3pm?", name="Ana")
    chat(client, b["key"], "Is the Camry available?")
    drain()
    with session_scope() as s:
        out = export_client(s, a["id"])
        assert out["conversations"] and out["customer_contacts"] and out["appointments"] and out["vehicles"]
        assert all(r["client_id"] == a["id"] for r in out["conversation_messages"])
    with session_scope() as s:
        path = delete_client(s, a["id"], "Alpha Auto", s.get(User, admin.id), str(tmp_path))
    assert json.load(open(path))["conversations"]
    with session_scope() as s:
        assert s.get(Client, a["id"]) is None
        assert s.scalar(select(func.count()).select_from(Conversation).where(Conversation.client_id == a["id"])) == 0
        assert s.scalar(select(func.count()).select_from(Vehicle).where(Vehicle.client_id == a["id"])) == 0
        assert s.scalar(select(func.count()).select_from(Conversation).where(Conversation.client_id == b["id"])) == 1  # untouched
        gone = s.scalars(select(User).where(User.email.like("deleted-user-%"))).all()
        assert len(gone) == 3 and all(not u.active and u.client_role == "deleted" and u.api_key_hash is None for u in gone)
    # A deleted dealership user's old API key no longer works.
    assert client.get(f"/api/v1/dealers/{a['id']}/conversations", headers=a["h"]["admin"]).status_code == 401

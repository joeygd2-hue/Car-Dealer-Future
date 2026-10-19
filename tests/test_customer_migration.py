"""Migration 0005 copies Phase 4's interim conversation rows into the Phase 3 tables without touching the originals."""

from __future__ import annotations

from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.conftest import PG_URL


@pytest.mark.skipif(bool(PG_URL), reason="builds its own SQLite database at an intermediate revision")
def test_interim_rows_are_copied_and_left_intact(tmp_path):
    from dealerflow.migrate import alembic_config

    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0004")
    eng = create_engine(url)
    now = datetime(2026, 9, 1, 12, 0)
    with eng.begin() as c:
        c.execute(text("INSERT INTO clients (id, name, slug, status, timezone, simulated, languages, business_hours, response_rules, "
                       "created_at, updated_at) VALUES (1, 'Alpha Motors (TEST)', 'alpha', 'active', 'America/Chicago', 1, '[]', '{}', "
                       "'{}', :n, :n)"), {"n": now})
        c.execute(text("INSERT INTO conversations (id, client_id, channel, customer_name, customer_contact, language, status, "
                       "lead_qualified, simulated, escalated_at, first_inbound_at, first_response_at, summary, created_at, updated_at) "
                       "VALUES (7, 1, 'web_form', 'Ana', 'ana@example.com', 'es', 'escalated', 1, 1, :n, :n, :n, 'old summary', :n, :n)"),
                  {"n": now})
        c.execute(text("INSERT INTO conversation_messages (id, client_id, conversation_id, direction, body, language, status, "
                       "provider_message_id, created_at) VALUES (1, 1, 7, 'inbound', 'Hola', 'es', 'received', 'pm-1', :n), "
                       "(2, 1, 7, 'outbound', 'Respuesta aprobada', 'es', 'sent_simulated', NULL, :n), "
                       "(3, 1, 7, 'outbound', 'Borrador', 'es', 'pending_approval', NULL, :n)"), {"n": now})
        c.execute(text("INSERT INTO appointments (id, client_id, conversation_id, kind, requested_at, scheduled_for, status, created_at, "
                       "updated_at) VALUES (4, 1, 7, 'test_drive', :n, :n, 'confirmed', :n, :n)"), {"n": now})

    command.upgrade(cfg, "head")
    with eng.connect() as c:
        conv = c.execute(text("SELECT * FROM customer_conversations")).mappings().one()
        assert (conv["legacy_conversation_id"], conv["channel"], conv["status"], conv["preferred_language"]) == (7, "WEBSITE_CHAT", "ESCALATED", "es")
        assert conv["simulated"] and conv["lead_qualified"] and conv["escalated_at"] is not None
        contact = c.execute(text("SELECT * FROM customer_contacts")).mappings().one()
        assert (contact["display_name"], contact["email"]) == ("Ana", "ana@example.com")
        msgs = c.execute(text("SELECT direction, sender_type, delivery_status, body FROM customer_messages ORDER BY id")).all()
        assert msgs == [("INBOUND", "CUSTOMER", "RECEIVED", "Hola"), ("OUTBOUND", "STAFF", "SIMULATED", "Respuesta aprobada")]
        draft = c.execute(text("SELECT status, body FROM response_drafts")).one()
        assert draft == ("PENDING_REVIEW", "Borrador")
        assert c.execute(text("SELECT status, legacy_appointment_id FROM test_drive_appointments")).one() == ("CONFIRMED", 4)
        assert c.execute(text("SELECT count(*) FROM escalations")).scalar() == 1
        # Originals untouched.
        assert c.execute(text("SELECT count(*) FROM conversation_messages")).scalar() == 3
        assert c.execute(text("SELECT summary FROM conversations WHERE id = 7")).scalar() == "old summary"

    command.downgrade(cfg, "0004")  # removes only what 0005 added
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM conversations")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM appointments")).scalar() == 1
    command.upgrade(cfg, "head")  # and the copy can run again from a clean 0004 state
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM customer_conversations")).scalar() == 1
    eng.dispose()

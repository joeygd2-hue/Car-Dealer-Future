"""The Alembic migrations produce exactly the schema the models describe."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from dealerflow.db import Base, get_engine


def test_migrations_match_models():
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"render_as_batch": True}), Base.metadata)
    assert diff == [], diff


def test_0004_moves_interim_conversations_into_phase3_tables_and_back(tmp_path):
    """Data written by the Phase 4 stand-in conversation tables survives the upgrade to the real Phase 3 tables
    (and the downgrade back). Runs on the suite's database engine type: SQLite file or the Postgres test URL."""
    from alembic import command
    from sqlalchemy import create_engine, text

    from dealerflow.migrate import alembic_config
    from tests.conftest import PG_URL

    url = PG_URL or f"sqlite:///{tmp_path}/mig.db"
    eng = create_engine(url)
    if PG_URL:
        with eng.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = alembic_config(url)
    command.upgrade(cfg, "0003")
    now = "CURRENT_TIMESTAMP"
    with eng.begin() as c:
        c.execute(text(f"INSERT INTO clients (name, slug, status, timezone, simulated, languages, business_hours, response_rules, "
                       f"created_at, updated_at) VALUES ('Alpha', 'alpha', 'active', 'America/Chicago', false, '[]', '{{}}', '{{}}', {now}, {now})"))
        c.execute(text(f"INSERT INTO users (email, name, role, password_hash, active, created_at, updated_at) "
                       f"VALUES ('rep@x.example', 'Rep', 'rep', 'x', true, {now}, {now})"))
        c.execute(text(
            "INSERT INTO conversations (id, client_id, channel, customer_name, customer_contact, language, status, lead_qualified, "
            "simulated, escalated_at, first_inbound_at, summary, created_at, updated_at) VALUES "
            f"(1, 1, 'web_form', 'Maria', 'maria@example.com', 'es', 'escalated', true, true, {now}, {now}, 'Asked about Civic', {now}, {now}),"
            f"(2, 1, 'web_form', NULL, '281-555-0100', 'en', 'open', false, false, NULL, {now}, NULL, {now}, {now})"))
        c.execute(text(
            "INSERT INTO conversation_messages (id, client_id, conversation_id, direction, body, language, status, approved_by_id, "
            "provider_message_id, created_at) VALUES "
            f"(1, 1, 1, 'inbound', 'Hola, prueba de manejo?', 'es', 'received', NULL, 'p-1', {now}),"
            f"(2, 1, 1, 'outbound', 'Con gusto...', 'es', 'sent_simulated', 1, NULL, {now}),"
            f"(3, 1, 2, 'outbound', 'Draft awaiting approval', 'en', 'pending_approval', NULL, NULL, {now})"))
        c.execute(text(
            "INSERT INTO appointments (id, client_id, conversation_id, kind, requested_at, scheduled_for, status, created_at, updated_at) "
            f"VALUES (1, 1, 1, 'test_drive', {now}, {now}, 'confirmed', {now}, {now}), (2, 1, 2, 'test_drive', {now}, NULL, 'requested', {now}, {now})"))
    command.upgrade(cfg, "head")
    with eng.begin() as c:
        convs = c.execute(text("SELECT id, channel, status, escalation_status, simulated, lead_qualified "
                               "FROM customer_conversations ORDER BY id")).all()
        assert [tuple(r) for r in convs] == [(1, "WEB_FORM", "ESCALATED", "OPEN", True, True), (2, "WEB_FORM", "OPEN", "NONE", False, False)]
        contacts = c.execute(text("SELECT display_name, email, phone FROM customer_contacts ORDER BY id")).all()
        assert [tuple(r) for r in contacts] == [("Maria", "maria@example.com", None), (None, None, "281-555-0100")]  # copied as entered
        msgs = c.execute(text("SELECT id, direction, sender_type, delivery_status FROM customer_messages ORDER BY id")).all()
        assert [tuple(r) for r in msgs] == [(1, "INBOUND", "CUSTOMER", "RECEIVED"), (2, "OUTBOUND", "STAFF", "SIMULATED")]
        drafts = c.execute(text("SELECT conversation_id, status, body FROM response_drafts")).all()
        assert [tuple(r) for r in drafts] == [(2, "PENDING_REVIEW", "Draft awaiting approval")]  # never auto-sent
        appts = c.execute(text("SELECT status, kind FROM customer_appointments ORDER BY id")).all()
        assert [tuple(r) for r in appts] == [("CONFIRMED", "TEST_DRIVE"), ("PENDING_CONFIRMATION", "TEST_DRIVE")]
        assert c.execute(text("SELECT count(*) FROM clients WHERE public_chat_key IS NULL")).scalar() == 0
    command.downgrade(cfg, "0003")
    with eng.begin() as c:
        rows = c.execute(text("SELECT id, channel, status, customer_name FROM conversations ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [(1, "web_form", "escalated", "Maria"), (2, "web_form", "open", None)]
        assert c.execute(text("SELECT count(*) FROM conversation_messages")).scalar() == 2  # migrated drafts are not restored
    command.upgrade(cfg, "head")
    eng.dispose()

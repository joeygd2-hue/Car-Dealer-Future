"""Migration 0005 converts the interim Phase 4 conversation tables without losing rows (SQLite, and Postgres when the
suite runs there), is re-runnable, keeps the legacy archive, and downgrades/re-upgrades cleanly."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from sqlalchemy import event

from dealerflow.config import get_settings
from dealerflow.legacy_conversations import convert
from dealerflow.migrate import alembic_config

SEED = [
    "insert into users (id,email,name,role,password_hash,active,created_at,updated_at) values "
    "(1,'s@x.example','s','admin','x',true,'2026-10-01','2026-10-01')",
    "insert into clients (id,name,slug,status,timezone,simulated,created_at,updated_at) values "
    "(1,'Old Dealer','old','active','America/Chicago',false,'2026-10-01','2026-10-01'),"
    "(2,'Other','other','active','America/Chicago',false,'2026-10-01','2026-10-01')",
    "insert into conversations (id,client_id,channel,customer_name,customer_contact,language,status,lead_qualified,simulated,"
    "first_inbound_at,first_response_at,summary,created_at,updated_at) values "
    "(1,1,'web_form','Ana Ruiz','ana@buyer.example','es','escalated',true,false,'2026-10-02','2026-10-02 00:05','Asked about financing','2026-10-02','2026-10-02'),"
    "(2,2,'web_form',null,'281-555-0199','en','open',false,true,'2026-10-03',null,null,'2026-10-03','2026-10-03')",
    "insert into conversation_messages (id,client_id,conversation_id,direction,body,language,status,approved_by_id,provider_message_id,"
    "created_at,sent_at) values (1,1,1,'inbound','Hola, financiamiento?','es','received',null,'p-1','2026-10-02',null),"
    "(2,1,1,'outbound','Gracias, le contactamos','es','sent_simulated',1,null,'2026-10-02 00:05','2026-10-02 00:05'),"
    "(3,2,2,'inbound','Is it available?','en','received',null,'p-2','2026-10-03',null),"
    "(4,2,2,'outbound','Draft reply','en','pending_approval',null,null,'2026-10-03',null)",
    "insert into appointments (id,client_id,conversation_id,kind,requested_at,scheduled_for,status,created_at,updated_at) values "
    "(1,1,1,'test_drive','2026-10-02','2026-10-05 15:00','requested','2026-10-02','2026-10-02')",
]


def test_interim_rows_survive_the_phase3_upgrade(tmp_path):
    url = get_settings().database_url
    if url.startswith("sqlite"):
        url = f"sqlite:///{tmp_path / 'mig.db'}"
    engine = sa.create_engine(url)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", lambda c, r: c.execute("PRAGMA foreign_keys=ON"))
    else:
        with engine.begin() as c:
            c.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = alembic_config(url)
    command.upgrade(cfg, "0004")
    with engine.begin() as c:
        for stmt in SEED:
            c.execute(sa.text(stmt))
    command.upgrade(cfg, "head")
    q = lambda c, s: c.execute(sa.text(s)).all()  # noqa: E731
    with engine.begin() as c:
        convs = q(c, "select id, client_id, channel, status, escalation_status from conversations order by id")
        assert convs == [(1, 1, "website_chat", "escalated", "open"), (2, 2, "website_chat", "awaiting_staff", "none")]
        assert q(c, "select client_id, display_name, email, phone from customer_contacts order by id") == [
            (1, "Ana Ruiz", "ana@buyer.example", None), (2, None, None, "281-555-0199")]
        assert [(r[0], r[1]) for r in q(c, "select id, delivery_status from conversation_messages order by id")] == [
            (1, "received"), (2, "simulated"), (3, "received")]
        assert q(c, "select conversation_id, status, body from response_drafts") == [(2, "pending_review", "Draft reply")]
        assert q(c, "select conversation_id, status from conversation_escalations") == [(1, "open")]
        assert q(c, "select id, contact_id, status from appointments") == [(1, 1, "requested")]
        assert c.execute(sa.text("select count(*) from legacy_conversations")).scalar() == 2  # archive kept
        assert c.execute(sa.text("select count(*) from customer_leads")).scalar() == 2
        try:  # composite foreign keys refuse a cross-dealership link
            with c.begin_nested():
                c.execute(sa.text("insert into conversation_messages (client_id,conversation_id,channel,direction,sender_type,body,"
                                  "delivery_status,analysis,created_at) values (2,1,'website_chat','inbound','customer','x','received','{}','2026-10-04')"))
            raise AssertionError("cross-dealership message accepted")
        except sa.exc.IntegrityError:
            pass
        assert convert(c) == dict(conversations=0, contacts=0, messages=0, drafts=0, escalations=0, appointments=0)  # idempotent
    command.check(cfg)
    command.downgrade(cfg, "0004")
    with engine.begin() as c:
        assert c.execute(sa.text("select count(*) from conversation_messages")).scalar() == 4  # interim tables back
    command.upgrade(cfg, "head")
    command.check(cfg)
    engine.dispose()

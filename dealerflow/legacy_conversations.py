"""Copy the interim Phase 4 conversation tables into the Phase 3 schema (migration 0005).

Migration 0005 renames the interim tables to ``legacy_conversations``, ``legacy_conversation_messages`` and
``legacy_appointments`` and keeps them as a read-only archive. ``convert(conn)`` copies their rows into the new
tables. It is idempotent: a row whose id already exists in the new table is skipped, so it is safe to run again
(``python -m dealerflow convert-legacy-conversations``) after an interrupted upgrade.

Mapping (interim → Phase 3):
- ``customer_name``/``customer_contact`` → a ``customer_contacts`` row (+ identity ``legacy:<id>`` and a lead);
  the contact value goes to ``email`` when it contains "@", otherwise to ``phone``.
- outbound messages still ``pending_approval`` were unsent drafts → ``response_drafts`` (status
  ``pending_review``); every other message keeps its id; ``sent_simulated`` → ``simulated``.
- ``escalated`` conversations get an open ``conversation_escalations`` row.
- appointments keep their id; ``scheduled_for`` → ``requested_start`` (and ``confirmed_start`` if confirmed).

Uses Core table definitions frozen here, not the ORM models, so later model changes cannot alter what this
migration step does. Nothing is deleted from the legacy tables.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection

md = sa.MetaData()

legacy_conv = sa.Table(
    "legacy_conversations", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("client_id", sa.Integer),
    sa.Column("channel", sa.String), sa.Column("customer_name", sa.String), sa.Column("customer_contact", sa.String),
    sa.Column("language", sa.String), sa.Column("vehicle_id", sa.Integer), sa.Column("status", sa.String),
    sa.Column("lead_qualified", sa.Boolean), sa.Column("simulated", sa.Boolean), sa.Column("escalated_at", sa.DateTime),
    sa.Column("first_inbound_at", sa.DateTime), sa.Column("first_response_at", sa.DateTime), sa.Column("summary", sa.Text),
    sa.Column("summary_delivered_at", sa.DateTime), sa.Column("pii_purged_at", sa.DateTime),
    sa.Column("created_at", sa.DateTime), sa.Column("updated_at", sa.DateTime),
)
legacy_msg = sa.Table(
    "legacy_conversation_messages", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("client_id", sa.Integer),
    sa.Column("conversation_id", sa.Integer), sa.Column("direction", sa.String), sa.Column("body", sa.Text),
    sa.Column("language", sa.String), sa.Column("status", sa.String), sa.Column("approved_by_id", sa.Integer),
    sa.Column("provider_message_id", sa.String), sa.Column("created_at", sa.DateTime), sa.Column("sent_at", sa.DateTime),
)
legacy_appt = sa.Table(
    "legacy_appointments", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("client_id", sa.Integer),
    sa.Column("conversation_id", sa.Integer), sa.Column("kind", sa.String), sa.Column("requested_at", sa.DateTime),
    sa.Column("scheduled_for", sa.DateTime), sa.Column("status", sa.String), sa.Column("created_at", sa.DateTime),
    sa.Column("updated_at", sa.DateTime),
)

contacts = sa.Table(
    "customer_contacts", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("client_id", sa.Integer),
    sa.Column("display_name", sa.String), sa.Column("email", sa.String), sa.Column("phone", sa.String),
    sa.Column("preferred_language", sa.String), sa.Column("contact_source", sa.String), sa.Column("simulated", sa.Boolean),
    sa.Column("pii_purged_at", sa.DateTime), sa.Column("created_at", sa.DateTime), sa.Column("updated_at", sa.DateTime),
)
identities = sa.Table(
    "customer_identities", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("client_id", sa.Integer),
    sa.Column("contact_id", sa.Integer), sa.Column("channel", sa.String), sa.Column("external_id", sa.String),
)
leads = sa.Table(
    "customer_leads", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("client_id", sa.Integer), sa.Column("contact_id", sa.Integer),
    sa.Column("vehicle_id", sa.Integer), sa.Column("source", sa.String), sa.Column("status", sa.String),
    sa.Column("simulated", sa.Boolean), sa.Column("created_at", sa.DateTime), sa.Column("updated_at", sa.DateTime),
)
convs = sa.Table(
    "conversations", md, sa.Column("id", sa.Integer, primary_key=True), *(sa.Column(c, sa.Integer) for c in ("client_id", "contact_id", "vehicle_id", "lead_id")),
    sa.Column("channel", sa.String), sa.Column("external_conversation_id", sa.String), sa.Column("preferred_language", sa.String),
    sa.Column("status", sa.String), sa.Column("human_takeover", sa.Boolean), sa.Column("opted_out", sa.Boolean),
    sa.Column("human_requested", sa.Boolean), sa.Column("lead_qualified", sa.Boolean), sa.Column("summary", sa.JSON),
    sa.Column("summary_delivered_at", sa.DateTime), sa.Column("unresolved_questions", sa.JSON),
    sa.Column("appointment_status", sa.String), sa.Column("escalation_status", sa.String), sa.Column("simulated", sa.Boolean),
    sa.Column("first_inbound_at", sa.DateTime), sa.Column("first_response_at", sa.DateTime),
    sa.Column("last_inbound_at", sa.DateTime), sa.Column("last_outbound_at", sa.DateTime),
    sa.Column("pii_purged_at", sa.DateTime), sa.Column("created_at", sa.DateTime), sa.Column("updated_at", sa.DateTime),
)
msgs = sa.Table(
    "conversation_messages", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("client_id", sa.Integer),
    sa.Column("conversation_id", sa.Integer), sa.Column("channel", sa.String), sa.Column("direction", sa.String),
    sa.Column("sender_type", sa.String), sa.Column("sender_user_id", sa.Integer), sa.Column("body", sa.Text),
    sa.Column("language", sa.String), sa.Column("provider_message_id", sa.String), sa.Column("delivery_status", sa.String),
    sa.Column("sent_at", sa.DateTime), sa.Column("analysis", sa.JSON), sa.Column("created_at", sa.DateTime),
)
drafts = sa.Table(
    "response_drafts", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("client_id", sa.Integer), sa.Column("conversation_id", sa.Integer),
    sa.Column("body", sa.Text), sa.Column("language", sa.String), sa.Column("status", sa.String),
    sa.Column("confidence", sa.Float), sa.Column("intents", sa.JSON), sa.Column("facts_used", sa.JSON),
    sa.Column("fact_snapshot", sa.JSON), sa.Column("unresolved", sa.JSON), sa.Column("escalation_reasons", sa.JSON),
    sa.Column("decision", sa.String), sa.Column("decision_reasons", sa.JSON), sa.Column("generated_by", sa.String),
    sa.Column("engine_version", sa.String), sa.Column("created_at", sa.DateTime), sa.Column("updated_at", sa.DateTime),
)
escalations = sa.Table(
    "conversation_escalations", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("client_id", sa.Integer),
    sa.Column("conversation_id", sa.Integer), sa.Column("reasons", sa.JSON), sa.Column("summary", sa.Text),
    sa.Column("recommended_action", sa.Text), sa.Column("status", sa.String), sa.Column("created_at", sa.DateTime),
    sa.Column("updated_at", sa.DateTime),
)
appts = sa.Table(
    "appointments", md, sa.Column("id", sa.Integer, primary_key=True),
    *(sa.Column(c, sa.Integer) for c in ("client_id", "conversation_id", "contact_id", "vehicle_id", "duration_minutes")),
    sa.Column("kind", sa.String), sa.Column("requested_at", sa.DateTime), sa.Column("requested_start", sa.DateTime),
    sa.Column("confirmed_start", sa.DateTime), sa.Column("status", sa.String), sa.Column("availability_checked", sa.String),
    sa.Column("notes", sa.Text), sa.Column("created_at", sa.DateTime), sa.Column("updated_at", sa.DateTime),
)

CHANNEL_MAP = {"web_form": "website_chat", "website": "website_chat", "chat": "website_chat", "website_chat": "website_chat",
               "adf": "adf_lead", "adf_lead": "adf_lead", "email": "adf_lead",
               "facebook_marketplace": "marketplace_assisted", "marketplace": "marketplace_assisted"}
STATUS_MAP = {"open": "awaiting_staff", "escalated": "escalated", "closed": "closed"}
DELIVERY_MAP = {"received": "received", "sent_simulated": "simulated", "sent": "sent", "failed": "failed"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def convert(conn: Connection) -> dict[str, int]:
    counts = dict(conversations=0, contacts=0, messages=0, drafts=0, escalations=0, appointments=0)
    existing_convs = set(conn.execute(sa.select(convs.c.id)).scalars())
    for c in conn.execute(sa.select(legacy_conv).order_by(legacy_conv.c.id)).mappings().all():
        if c["id"] in existing_convs:
            continue
        now = _now()
        created = c["created_at"] or now
        channel = CHANNEL_MAP.get((c["channel"] or "").lower(), (c["channel"] or "unknown").lower()[:40])
        value = (c["customer_contact"] or "").strip()
        # A previous interrupted run may already have created the contact (identity legacy:<id>) and lead.
        contact_id = conn.execute(sa.select(identities.c.contact_id).where(
            identities.c.client_id == c["client_id"], identities.c.channel == channel,
            identities.c.external_id == f"legacy:{c['id']}")).scalar()
        if contact_id is None:
            contact_id = conn.execute(contacts.insert().values(
                client_id=c["client_id"], display_name=c["customer_name"], email=value.lower() if "@" in value else None,
                phone=value if value and "@" not in value else None, preferred_language=c["language"],
                contact_source="converted from interim conversation", simulated=bool(c["simulated"]),
                pii_purged_at=c["pii_purged_at"], created_at=created, updated_at=now)).inserted_primary_key[0]
            counts["contacts"] += 1
            conn.execute(identities.insert().values(client_id=c["client_id"], contact_id=contact_id, channel=channel,
                                                    external_id=f"legacy:{c['id']}"))
        lead_id = conn.execute(sa.select(leads.c.id).where(leads.c.client_id == c["client_id"],
                                                           leads.c.contact_id == contact_id)).scalar()
        if lead_id is None:
            lead_id = conn.execute(leads.insert().values(
                client_id=c["client_id"], contact_id=contact_id, vehicle_id=c["vehicle_id"], source=channel,
                status="appointment" if c["lead_qualified"] else "new", simulated=bool(c["simulated"]),
                created_at=created, updated_at=now)).inserted_primary_key[0]
        rows = conn.execute(sa.select(legacy_msg).where(legacy_msg.c.conversation_id == c["id"])
                            .order_by(legacy_msg.c.id)).mappings().all()
        inbound = [m["created_at"] for m in rows if m["direction"] == "inbound"]
        outbound = [m["sent_at"] or m["created_at"] for m in rows if m["direction"] == "outbound" and m["status"] != "pending_approval"]
        escalated = c["status"] == "escalated"
        conn.execute(convs.insert().values(
            id=c["id"], client_id=c["client_id"], channel=channel, external_conversation_id=f"legacy:{c['id']}",
            contact_id=contact_id, vehicle_id=c["vehicle_id"], lead_id=lead_id, preferred_language=c["language"] or "en",
            status=STATUS_MAP.get(c["status"], "open"), human_takeover=False, opted_out=False, human_requested=False,
            lead_qualified=bool(c["lead_qualified"]), summary={"legacy_summary": c["summary"]} if c["summary"] else {},
            summary_delivered_at=c["summary_delivered_at"], unresolved_questions=[], appointment_status="none",
            escalation_status="open" if escalated else "none", simulated=bool(c["simulated"]),
            first_inbound_at=c["first_inbound_at"], first_response_at=c["first_response_at"],
            last_inbound_at=max(inbound) if inbound else c["first_inbound_at"], last_outbound_at=max(outbound) if outbound else None,
            pii_purged_at=c["pii_purged_at"], created_at=created, updated_at=c["updated_at"] or now))
        counts["conversations"] += 1
        for m in rows:
            if m["direction"] == "outbound" and m["status"] == "pending_approval":
                conn.execute(drafts.insert().values(
                    client_id=c["client_id"], conversation_id=c["id"], body=m["body"], language=m["language"] or "en",
                    status="pending_review", confidence=0.0, intents=[], facts_used=[], fact_snapshot={}, unresolved=[],
                    escalation_reasons=[], decision="draft_only",
                    decision_reasons=["converted from an interim approved-response draft; review before sending"],
                    generated_by="legacy", engine_version="legacy", created_at=m["created_at"] or now, updated_at=now))
                counts["drafts"] += 1
                continue
            conn.execute(msgs.insert().values(
                id=m["id"], client_id=c["client_id"], conversation_id=c["id"], channel=channel, direction=m["direction"],
                sender_type="customer" if m["direction"] == "inbound" else "staff", sender_user_id=m["approved_by_id"],
                body=m["body"], language=m["language"], provider_message_id=m["provider_message_id"],
                delivery_status=DELIVERY_MAP.get(m["status"], (m["status"] or "received")[:20]), sent_at=m["sent_at"],
                analysis={}, created_at=m["created_at"] or now))
            counts["messages"] += 1
        if escalated:
            conn.execute(escalations.insert().values(
                client_id=c["client_id"], conversation_id=c["id"], reasons=["legacy_escalation"],
                summary=c["summary"] or "", recommended_action="Review this conversation (escalated before the Phase 3 upgrade).",
                status="open", created_at=c["escalated_at"] or created, updated_at=now))
            counts["escalations"] += 1
    existing_appts = set(conn.execute(sa.select(appts.c.id)).scalars())
    contact_of = dict(conn.execute(sa.select(convs.c.id, convs.c.contact_id)).all())
    vehicle_of = dict(conn.execute(sa.select(convs.c.id, convs.c.vehicle_id)).all())
    for a in conn.execute(sa.select(legacy_appt).order_by(legacy_appt.c.id)).mappings().all():
        if a["id"] in existing_appts:
            continue
        status = a["status"] if a["status"] in ("requested", "confirmed", "cancelled", "completed", "no_show") else "requested"
        conn.execute(appts.insert().values(
            id=a["id"], client_id=a["client_id"], conversation_id=a["conversation_id"],
            contact_id=contact_of.get(a["conversation_id"]), vehicle_id=vehicle_of.get(a["conversation_id"]),
            duration_minutes=30, kind=a["kind"] or "test_drive", requested_at=a["requested_at"] or a["created_at"],
            requested_start=a["scheduled_for"], confirmed_start=a["scheduled_for"] if status == "confirmed" else None,
            status=status, availability_checked="not_checked", notes="converted from interim appointment",
            created_at=a["created_at"], updated_at=a["updated_at"] or _now()))
        counts["appointments"] += 1
    if conn.dialect.name == "postgresql":  # explicit ids were inserted: move the sequences past them
        for table in ("conversations", "conversation_messages", "appointments"):
            conn.execute(sa.text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                                 f"GREATEST(COALESCE((SELECT MAX(id) FROM {table}), 0), 1), "
                                 f"(SELECT COUNT(*) > 0 FROM {table}))"))
    return counts

"""Phase 3 customer conversations (inbox, drafts, escalations, test drives, response automation, send ledger).

Replaces Phase 4's interim stand-ins (``conversations``, ``conversation_messages``, ``appointments``) with the
Phase 3 schema on the shared ``clients`` / ``vehicles`` tables. Existing interim rows are copied, not discarded:

- every interim conversation becomes a ``customer_conversations`` row with the same id; its customer name /
  contact becomes a ``customer_contacts`` row. Unknown interim channel names are kept (upper-cased), so they are
  not sendable channels and nothing is ever sent automatically for a migrated conversation;
- interim messages keep their id; outbound messages still awaiting approval become PENDING_REVIEW drafts;
- interim appointments keep their id; "requested" becomes PENDING_CONFIRMATION (staff must still confirm).

Row counts are checked before the interim tables are dropped; the migration aborts if anything is missing.
Downgrade recreates the interim tables and copies the same fields back (Phase 3-only data is lost).

Also adds Phase 3 columns to ``clients`` (public chat key, business-info approval), ``integration_connections``
(paused, external_account_id, config) and ``vehicles`` (verified_at, data_conflict_note).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19
"""
import json
import secrets

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

STATUS_CONV = {"open": "OPEN", "escalated": "ESCALATED", "closed": "CLOSED"}
STATUS_APPT = {"requested": "PENDING_CONFIRMATION", "confirmed": "CONFIRMED", "cancelled": "CANCELLED",
               "completed": "COMPLETED", "no_show": "NO_SHOW"}
STATUS_MSG = {"received": "RECEIVED", "sent_simulated": "SIMULATED", "sent": "SENT", "failed": "FAILED"}
KNOWN_CHANNELS = {"WEBSITE_CHAT", "META_MESSENGER", "TWILIO_SMS", "ADF_LEAD", "MARKETPLACE_ASSISTED"}


def _interim_tables(meta):
    conv = sa.Table('conversations', meta,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('channel', sa.String(length=30), nullable=False),
        sa.Column('customer_name', sa.String(length=200), nullable=True),
        sa.Column('customer_contact', sa.String(length=255), nullable=True),
        sa.Column('language', sa.String(length=5), nullable=False),
        sa.Column('vehicle_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('lead_qualified', sa.Boolean(), nullable=False),
        sa.Column('simulated', sa.Boolean(), nullable=False),
        sa.Column('escalated_at', sa.DateTime(), nullable=True),
        sa.Column('first_inbound_at', sa.DateTime(), nullable=True),
        sa.Column('first_response_at', sa.DateTime(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('summary_delivered_at', sa.DateTime(), nullable=True),
        sa.Column('pii_purged_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'))
    msgs = sa.Table('conversation_messages', meta,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        sa.Column('direction', sa.String(length=10), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('language', sa.String(length=5), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('approved_by_id', sa.Integer(), nullable=True),
        sa.Column('provider_message_id', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['approved_by_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider_message_id'))
    appts = sa.Table('appointments', meta,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('requested_at', sa.DateTime(), nullable=False),
        sa.Column('scheduled_for', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("status in ('requested','confirmed','cancelled','completed','no_show')", name='ck_appointments_status'),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'))
    return conv, msgs, appts


def _new_tables(meta):
    t = {}
    for name in ('customer_contacts', 'customer_conversations', 'customer_messages', 'customer_appointments', 'response_drafts'):
        t[name] = sa.Table(name, meta, autoload_with=op.get_bind())
    return t


def _bump_sequences(bind, tables):
    if bind.dialect.name != 'postgresql':
        return
    for name in tables:
        bind.execute(sa.text(f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                             f"COALESCE((SELECT MAX(id) FROM {name}), 0) + 1, false)"))


def _copy_interim_to_phase3():
    bind = op.get_bind()
    meta = sa.MetaData()
    conv_t, msg_t, appt_t = _interim_tables(meta)
    new = _new_tables(sa.MetaData())
    convs = bind.execute(sa.select(conv_t)).mappings().all()
    msgs = bind.execute(sa.select(msg_t)).mappings().all()
    appts = bind.execute(sa.select(appt_t)).mappings().all()
    contact_of, channel_of = {}, {}
    for c in convs:
        contact_id = None
        if c['customer_name'] or c['customer_contact']:
            raw = (c['customer_contact'] or '').strip()
            res = bind.execute(new['customer_contacts'].insert().values(
                client_id=c['client_id'], display_name=c['customer_name'], email=raw.lower() if '@' in raw else None,
                phone=raw if raw and '@' not in raw else None, preferred_language=c['language'],
                contact_source='migrated from the Phase 4 interim inquiry record', sms_consent=False,
                created_at=c['created_at'], updated_at=c['updated_at']))
            contact_id = res.inserted_primary_key[0]
        channel = (c['channel'] or 'UNKNOWN').upper()
        contact_of[c['id']], channel_of[c['id']] = contact_id, channel
        escalated = c['status'] == 'escalated' or c['escalated_at'] is not None
        bind.execute(new['customer_conversations'].insert().values(
            id=c['id'], client_id=c['client_id'], channel=channel, external_conversation_id=f"interim:{c['id']}",
            contact_id=contact_id, vehicle_id=c['vehicle_id'], preferred_language=c['language'] or 'en',
            status=STATUS_CONV.get(c['status'], 'OPEN'), human_takeover=False, opted_out=False, human_requested=False,
            summary={"legacy_summary": c['summary']} if c['summary'] else {}, unresolved_questions=[],
            appointment_status='NONE', escalation_status='OPEN' if escalated else 'NONE',
            last_inbound_at=c['first_inbound_at'], simulated=bool(c['simulated']), lead_qualified=bool(c['lead_qualified']),
            first_inbound_at=c['first_inbound_at'], first_response_at=c['first_response_at'], escalated_at=c['escalated_at'],
            summary_delivered_at=c['summary_delivered_at'], pii_purged_at=c['pii_purged_at'],
            created_at=c['created_at'], updated_at=c['updated_at']))
    drafts = 0
    for m in msgs:
        if m['direction'] == 'outbound' and m['status'] == 'pending_approval':
            bind.execute(new['response_drafts'].insert().values(
                client_id=m['client_id'], conversation_id=m['conversation_id'], body=m['body'], language=m['language'] or 'en',
                status='PENDING_REVIEW', confidence=0.0, intents=[], facts_used=[], fact_snapshot={}, unresolved=[],
                escalation_reasons=[], decision='DRAFT_ONLY', decision_reasons=["migrated interim draft: review before sending"],
                generated_by='interim_template', engine_version='interim', created_at=m['created_at'], updated_at=m['created_at']))
            drafts += 1
            continue
        inbound = m['direction'] == 'inbound'
        bind.execute(new['customer_messages'].insert().values(
            id=m['id'], client_id=m['client_id'], conversation_id=m['conversation_id'], channel=channel_of.get(m['conversation_id'], 'UNKNOWN'),
            direction='INBOUND' if inbound else 'OUTBOUND', sender_type='CUSTOMER' if inbound else 'STAFF',
            sender_user_id=m['approved_by_id'], body=m['body'], language=m['language'], provider_message_id=m['provider_message_id'],
            created_at=m['created_at'], delivery_status=STATUS_MSG.get(m['status'], (m['status'] or 'RECEIVED').upper()[:20]),
            analysis={"migrated_from": "interim"}))
    for a in appts:
        status = STATUS_APPT.get(a['status'], 'PENDING_CONFIRMATION')
        bind.execute(new['customer_appointments'].insert().values(
            id=a['id'], client_id=a['client_id'], conversation_id=a['conversation_id'],
            contact_id=contact_of.get(a['conversation_id']), kind='TEST_DRIVE', requested_start=a['scheduled_for'],
            duration_minutes=30, confirmed_start=a['scheduled_for'] if status == 'CONFIRMED' else None, status=status,
            availability_checked='NOT_CHECKED', notes='migrated from the Phase 4 interim appointment record',
            created_at=a['created_at'], updated_at=a['updated_at']))
    _bump_sequences(bind, ['customer_contacts', 'customer_conversations', 'customer_messages', 'customer_appointments', 'response_drafts'])
    count = lambda t: bind.execute(sa.select(sa.func.count()).select_from(new[t])).scalar()  # noqa: E731
    if (count('customer_conversations') != len(convs) or count('customer_messages') + drafts != len(msgs)
            or count('customer_appointments') != len(appts)):
        raise RuntimeError("interim conversation data was not copied completely; aborting before dropping it")


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('rate_counters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('window_start', sa.DateTime(), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key', 'window_start', name='uq_rate_counter_window')
    )
    op.create_table('sandbox_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('consumed', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sandbox_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sandbox_records_kind'), ['kind'], unique=False)

    op.create_table('pilot_recipients',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('value', sa.String(length=200), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=False),
    sa.Column('added_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['added_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('kind', 'value', name='uq_pilot_recipient')
    )
    op.create_table('approved_templates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('intent', sa.String(length=40), nullable=False),
    sa.Column('language', sa.String(length=5), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('auto_send_allowed', sa.Boolean(), nullable=False),
    sa.Column('approved_by_id', sa.Integer(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['approved_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('approved_templates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_approved_templates_client_id'), ['client_id'], unique=False)

    op.create_table('contact_suppressions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('address', sa.String(length=200), nullable=False),
    sa.Column('reason', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'channel', 'address', name='uq_contact_suppression')
    )
    with op.batch_alter_table('contact_suppressions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_contact_suppressions_client_id'), ['client_id'], unique=False)

    op.create_table('customer_contacts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=True),
    sa.Column('email', sa.String(length=200), nullable=True),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('preferred_language', sa.String(length=5), nullable=True),
    sa.Column('contact_source', sa.String(length=100), nullable=True),
    sa.Column('sms_consent', sa.Boolean(), nullable=False),
    sa.Column('sms_consent_source', sa.Text(), nullable=True),
    sa.Column('opted_out_at', sa.DateTime(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'id', name='uq_customer_contacts_client_id')
    )
    with op.batch_alter_table('customer_contacts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_contacts_client_id'), ['client_id'], unique=False)

    op.create_table('evaluation_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=True),
    sa.Column('engine_version', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('total_cases', sa.Integer(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('failures', sa.JSON(), nullable=False),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('triggered_by_id', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['triggered_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('evaluation_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_evaluation_runs_client_id'), ['client_id'], unique=False)

    op.create_table('faqs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('language', sa.String(length=5), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('answer', sa.Text(), nullable=False),
    sa.Column('keywords', sa.JSON(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('auto_send_allowed', sa.Boolean(), nullable=False),
    sa.Column('approved_by_id', sa.Integer(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['approved_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('faqs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_faqs_client_id'), ['client_id'], unique=False)

    op.create_table('inbound_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=False),
    sa.Column('event_id', sa.String(length=200), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=True),
    sa.Column('received_at', sa.DateTime(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'event_id', name='uq_inbound_event')
    )
    with op.batch_alter_table('inbound_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_inbound_events_client_id'), ['client_id'], unique=False)

    op.create_table('notification_preferences',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('immediate_alerts', sa.Boolean(), nullable=False),
    sa.Column('daily_digest', sa.Boolean(), nullable=False),
    sa.Column('digest_hour_local', sa.Integer(), nullable=False),
    sa.Column('notify_assigned_user', sa.Boolean(), nullable=False),
    sa.Column('email_enabled', sa.Boolean(), nullable=False),
    sa.Column('escalate_to_admins_reasons', sa.JSON(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', name='uq_notification_preferences_client')
    )
    with op.batch_alter_table('notification_preferences', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_preferences_client_id'), ['client_id'], unique=False)

    op.create_table('notifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('recipient_address', sa.String(length=200), nullable=True),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('subject', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('link', sa.String(length=400), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('status_detail', sa.Text(), nullable=True),
    sa.Column('provider_message_id', sa.String(length=200), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=200), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('read_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notifications_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_notifications_user_id'), ['user_id'], unique=False)

    op.create_table('response_automation_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(length=30), nullable=False),
    sa.Column('paused', sa.Boolean(), nullable=False),
    sa.Column('paused_reason', sa.Text(), nullable=True),
    sa.Column('paused_by_id', sa.Integer(), nullable=True),
    sa.Column('paused_at', sa.DateTime(), nullable=True),
    sa.Column('max_auto_replies_per_conversation_per_hour', sa.Integer(), nullable=False),
    sa.Column('max_auto_replies_per_dealership_per_hour', sa.Integer(), nullable=False),
    sa.Column('min_seconds_between_auto_replies', sa.Integer(), nullable=False),
    sa.Column('auto_reply_outside_business_hours', sa.Boolean(), nullable=False),
    sa.Column('stale_inventory_hours', sa.Integer(), nullable=False),
    sa.Column('confidence_threshold', sa.Float(), nullable=False),
    sa.Column('controlled_intents', sa.JSON(), nullable=False),
    sa.Column('escalation_emails', sa.JSON(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['paused_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', name='uq_response_automation_client')
    )
    with op.batch_alter_table('response_automation_settings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_response_automation_settings_client_id'), ['client_id'], unique=False)

    op.create_table('send_attempts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('ref_id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=True),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('recipient', sa.String(length=200), nullable=False),
    sa.Column('live', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('reasons', sa.JSON(), nullable=False),
    sa.Column('provider_message_id', sa.String(length=300), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('send_attempts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_send_attempts_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_send_attempts_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_send_attempts_ref_id'), ['ref_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_send_attempts_status'), ['status'], unique=False)

    op.create_table('customer_identities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id', 'contact_id'], ['customer_contacts.client_id', 'customer_contacts.id'], name='fk_customer_identities_contact'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'channel', 'external_id', name='uq_customer_identity')
    )
    with op.batch_alter_table('customer_identities', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_identities_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_identities_contact_id'), ['contact_id'], unique=False)

    op.create_table('mode_authorizations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(length=30), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('authorized_by_id', sa.Integer(), nullable=False),
    sa.Column('authorized_at', sa.DateTime(), nullable=False),
    sa.Column('evaluation_run_id', sa.Integer(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['authorized_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['evaluation_run_id'], ['evaluation_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('mode_authorizations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_mode_authorizations_client_id'), ['client_id'], unique=False)

    op.create_table('customer_leads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=False),
    sa.Column('vehicle_id', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('assigned_user_id', sa.Integer(), nullable=True),
    sa.Column('external_ref', sa.String(length=200), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id', 'contact_id'], ['customer_contacts.client_id', 'customer_contacts.id'], name='fk_customer_leads_contact'),
    sa.ForeignKeyConstraint(['client_id', 'vehicle_id'], ['vehicles.client_id', 'vehicles.id'], name='fk_customer_leads_vehicle'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'id', name='uq_customer_leads_client_id')
    )
    with op.batch_alter_table('customer_leads', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_leads_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_leads_contact_id'), ['contact_id'], unique=False)

    op.create_table('customer_conversations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('external_conversation_id', sa.String(length=200), nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=True),
    sa.Column('vehicle_id', sa.Integer(), nullable=True),
    sa.Column('lead_id', sa.Integer(), nullable=True),
    sa.Column('preferred_language', sa.String(length=5), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('assigned_user_id', sa.Integer(), nullable=True),
    sa.Column('human_takeover', sa.Boolean(), nullable=False),
    sa.Column('takeover_by_id', sa.Integer(), nullable=True),
    sa.Column('takeover_at', sa.DateTime(), nullable=True),
    sa.Column('opted_out', sa.Boolean(), nullable=False),
    sa.Column('opted_out_at', sa.DateTime(), nullable=True),
    sa.Column('human_requested', sa.Boolean(), nullable=False),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('summary_updated_at', sa.DateTime(), nullable=True),
    sa.Column('unresolved_questions', sa.JSON(), nullable=False),
    sa.Column('next_action', sa.Text(), nullable=True),
    sa.Column('appointment_status', sa.String(length=30), nullable=False),
    sa.Column('escalation_status', sa.String(length=20), nullable=False),
    sa.Column('last_inbound_at', sa.DateTime(), nullable=True),
    sa.Column('last_outbound_at', sa.DateTime(), nullable=True),
    sa.Column('simulated', sa.Boolean(), nullable=False),
    sa.Column('lead_qualified', sa.Boolean(), nullable=False),
    sa.Column('first_inbound_at', sa.DateTime(), nullable=True),
    sa.Column('first_response_at', sa.DateTime(), nullable=True),
    sa.Column('escalated_at', sa.DateTime(), nullable=True),
    sa.Column('summary_delivered_at', sa.DateTime(), nullable=True),
    sa.Column('pii_purged_at', sa.DateTime(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id', 'contact_id'], ['customer_contacts.client_id', 'customer_contacts.id'], name='fk_customer_conversations_contact'),
    sa.ForeignKeyConstraint(['client_id', 'lead_id'], ['customer_leads.client_id', 'customer_leads.id'], name='fk_customer_conversations_lead'),
    sa.ForeignKeyConstraint(['client_id', 'vehicle_id'], ['vehicles.client_id', 'vehicles.id'], name='fk_customer_conversations_vehicle'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['takeover_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'channel', 'external_conversation_id', name='uq_customer_conversation_external'),
    sa.UniqueConstraint('client_id', 'id', name='uq_customer_conversations_client_id')
    )
    with op.batch_alter_table('customer_conversations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_conversations_client_id'), ['client_id'], unique=False)
        batch_op.create_index('ix_customer_conversations_client_status', ['client_id', 'status'], unique=False)

    op.create_table('customer_appointments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=True),
    sa.Column('contact_id', sa.Integer(), nullable=True),
    sa.Column('vehicle_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('customer_name', sa.String(length=200), nullable=True),
    sa.Column('customer_phone', sa.String(length=40), nullable=True),
    sa.Column('customer_email', sa.String(length=200), nullable=True),
    sa.Column('requested_start', sa.DateTime(), nullable=True),
    sa.Column('duration_minutes', sa.Integer(), nullable=False),
    sa.Column('confirmed_start', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('assigned_user_id', sa.Integer(), nullable=True),
    sa.Column('calendar_provider', sa.String(length=40), nullable=True),
    sa.Column('calendar_event_id', sa.String(length=200), nullable=True),
    sa.Column('availability_checked', sa.String(length=30), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id', 'contact_id'], ['customer_contacts.client_id', 'customer_contacts.id'], name='fk_customer_appointments_contact'),
    sa.ForeignKeyConstraint(['client_id', 'conversation_id'], ['customer_conversations.client_id', 'customer_conversations.id'], name='fk_customer_appointments_conversation'),
    sa.ForeignKeyConstraint(['client_id', 'vehicle_id'], ['vehicles.client_id', 'vehicles.id'], name='fk_customer_appointments_vehicle'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'id', name='uq_customer_appointments_client_id')
    )
    with op.batch_alter_table('customer_appointments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_appointments_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_appointments_conversation_id'), ['conversation_id'], unique=False)

    op.create_table('customer_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('sender_type', sa.String(length=20), nullable=False),
    sa.Column('sender_user_id', sa.Integer(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=5), nullable=True),
    sa.Column('provider_message_id', sa.String(length=200), nullable=True),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('delivery_status', sa.String(length=20), nullable=False),
    sa.Column('delivery_error', sa.Text(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=200), nullable=True),
    sa.Column('analysis', sa.JSON(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id', 'conversation_id'], ['customer_conversations.client_id', 'customer_conversations.id'], name='fk_customer_messages_conversation'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['sender_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel', 'provider_message_id', name='uq_customer_message_provider_id'),
    sa.UniqueConstraint('client_id', 'id', name='uq_customer_messages_client_id'),
    sa.UniqueConstraint('idempotency_key')
    )
    with op.batch_alter_table('customer_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_messages_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_messages_conversation_id'), ['conversation_id'], unique=False)

    op.create_table('escalations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('reasons', sa.JSON(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('recommended_action', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('assigned_user_id', sa.Integer(), nullable=True),
    sa.Column('resolved_by_id', sa.Integer(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id', 'conversation_id'], ['customer_conversations.client_id', 'customer_conversations.id'], name='fk_escalations_conversation'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['resolved_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('escalations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_escalations_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_escalations_conversation_id'), ['conversation_id'], unique=False)

    op.create_table('appointment_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('appointment_id', sa.Integer(), nullable=False),
    sa.Column('event', sa.String(length=40), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('actor', sa.String(length=60), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id', 'appointment_id'], ['customer_appointments.client_id', 'customer_appointments.id'], name='fk_appointment_events_appointment'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('appointment_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_appointment_events_appointment_id'), ['appointment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_appointment_events_client_id'), ['client_id'], unique=False)

    op.create_table('response_drafts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('in_reply_to_message_id', sa.Integer(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=5), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('intents', sa.JSON(), nullable=False),
    sa.Column('facts_used', sa.JSON(), nullable=False),
    sa.Column('fact_snapshot', sa.JSON(), nullable=False),
    sa.Column('unresolved', sa.JSON(), nullable=False),
    sa.Column('escalation_reasons', sa.JSON(), nullable=False),
    sa.Column('decision', sa.String(length=40), nullable=True),
    sa.Column('decision_reasons', sa.JSON(), nullable=False),
    sa.Column('generated_by', sa.String(length=40), nullable=False),
    sa.Column('engine_version', sa.String(length=20), nullable=False),
    sa.Column('reviewed_by_id', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('sent_message_id', sa.Integer(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id', 'conversation_id'], ['customer_conversations.client_id', 'customer_conversations.id'], name='fk_response_drafts_conversation'),
    sa.ForeignKeyConstraint(['client_id', 'in_reply_to_message_id'], ['customer_messages.client_id', 'customer_messages.id'], name='fk_response_drafts_in_reply_to'),
    sa.ForeignKeyConstraint(['client_id', 'sent_message_id'], ['customer_messages.client_id', 'customer_messages.id'], name='fk_response_drafts_sent_message'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['reviewed_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('response_drafts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_response_drafts_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_response_drafts_conversation_id'), ['conversation_id'], unique=False)

    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('public_chat_key', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('business_info_approved', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('business_info_approved_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('business_info_approved_by_id', sa.Integer(), nullable=True))
    bind = op.get_bind()
    for (cid,) in bind.execute(sa.text("SELECT id FROM clients")).all():
        bind.execute(sa.text("UPDATE clients SET public_chat_key = :k WHERE id = :i"), {"k": secrets.token_urlsafe(24), "i": cid})
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.alter_column('public_chat_key', existing_type=sa.String(length=64), nullable=False)
        batch_op.create_unique_constraint('uq_clients_public_chat_key', ['public_chat_key'])
        batch_op.create_foreign_key('fk_clients_business_info_approved_by', 'users', ['business_info_approved_by_id'], ['id'])

    with op.batch_alter_table('integration_connections', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paused', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('external_account_id', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('config', sa.JSON(), nullable=False, server_default=sa.text("'{}'")))

    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('verified_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('data_conflict_note', sa.Text(), nullable=True))

    _copy_interim_to_phase3()

    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_appointments_client_id'))
    op.drop_table('appointments')
    with op.batch_alter_table('conversation_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conversation_messages_client_id'))
        batch_op.drop_index(batch_op.f('ix_conversation_messages_conversation_id'))
    op.drop_table('conversation_messages')
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conversations_client_id'))
    op.drop_table('conversations')


def _copy_phase3_to_interim(conv_t, msg_t, appt_t):
    bind = op.get_bind()
    new = _new_tables(sa.MetaData())
    contacts = {r['id']: r for r in bind.execute(sa.select(new['customer_contacts'])).mappings()}
    inv_status = {v: k for k, v in STATUS_CONV.items()}
    for c in bind.execute(sa.select(new['customer_conversations'])).mappings():
        contact = contacts.get(c['contact_id']) or {}
        summary = c['summary'] or {}
        if isinstance(summary, str):
            summary = json.loads(summary)
        bind.execute(conv_t.insert().values(
            id=c['id'], client_id=c['client_id'], channel=c['channel'].lower()[:30], customer_name=contact.get('display_name'),
            customer_contact=contact.get('email') or contact.get('phone'), language=c['preferred_language'],
            vehicle_id=c['vehicle_id'], status=inv_status.get(c['status'], 'escalated' if c['escalation_status'] == 'OPEN' else 'open'),
            lead_qualified=c['lead_qualified'], simulated=c['simulated'], escalated_at=c['escalated_at'],
            first_inbound_at=c['first_inbound_at'], first_response_at=c['first_response_at'],
            summary=summary.get('legacy_summary') or summary.get('recommended_next_step'),
            summary_delivered_at=c['summary_delivered_at'], pii_purged_at=c['pii_purged_at'],
            created_at=c['created_at'], updated_at=c['updated_at']))
    for m in bind.execute(sa.select(new['customer_messages'])).mappings():
        bind.execute(msg_t.insert().values(
            id=m['id'], client_id=m['client_id'], conversation_id=m['conversation_id'], direction=m['direction'].lower()[:10],
            body=m['body'], language=m['language'] or 'en',
            status={"SIMULATED": "sent_simulated", "RECEIVED": "received"}.get(m['delivery_status'], m['delivery_status'].lower()),
            approved_by_id=m['sender_user_id'], provider_message_id=m['provider_message_id'], created_at=m['created_at']))
    inv_appt = {v: k for k, v in STATUS_APPT.items()}
    for a in bind.execute(sa.select(new['customer_appointments'])).mappings():
        bind.execute(appt_t.insert().values(
            id=a['id'], client_id=a['client_id'], conversation_id=a['conversation_id'], kind='test_drive',
            requested_at=a['created_at'], scheduled_for=a['confirmed_start'] or a['requested_start'],
            status=inv_appt.get(a['status'], 'cancelled' if a['status'] == 'DECLINED' else 'requested'),
            created_at=a['created_at'], updated_at=a['updated_at']))


def downgrade() -> None:
    meta = sa.MetaData()
    for referenced in ('clients', 'vehicles', 'users'):
        sa.Table(referenced, meta, autoload_with=op.get_bind())
    conv_t, msg_t, appt_t = _interim_tables(meta)
    meta.create_all(op.get_bind(), tables=[conv_t, msg_t, appt_t])
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversations_client_id'), ['client_id'], unique=False)
    with op.batch_alter_table('conversation_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversation_messages_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_messages_conversation_id'), ['conversation_id'], unique=False)
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_appointments_client_id'), ['client_id'], unique=False)
    _copy_phase3_to_interim(conv_t, msg_t, appt_t)

    op.drop_table('response_drafts')
    with op.batch_alter_table('appointment_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_appointment_events_client_id'))
        batch_op.drop_index(batch_op.f('ix_appointment_events_appointment_id'))

    op.drop_table('appointment_events')
    with op.batch_alter_table('escalations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_escalations_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_escalations_client_id'))

    op.drop_table('escalations')
    with op.batch_alter_table('customer_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_messages_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_customer_messages_client_id'))

    op.drop_table('customer_messages')
    with op.batch_alter_table('customer_appointments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_appointments_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_customer_appointments_client_id'))

    op.drop_table('customer_appointments')
    with op.batch_alter_table('customer_conversations', schema=None) as batch_op:
        batch_op.drop_index('ix_customer_conversations_client_status')
        batch_op.drop_index(batch_op.f('ix_customer_conversations_client_id'))

    op.drop_table('customer_conversations')
    with op.batch_alter_table('customer_leads', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_leads_contact_id'))
        batch_op.drop_index(batch_op.f('ix_customer_leads_client_id'))

    op.drop_table('customer_leads')
    with op.batch_alter_table('mode_authorizations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mode_authorizations_client_id'))

    op.drop_table('mode_authorizations')
    with op.batch_alter_table('customer_identities', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_identities_contact_id'))
        batch_op.drop_index(batch_op.f('ix_customer_identities_client_id'))

    op.drop_table('customer_identities')
    with op.batch_alter_table('send_attempts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_send_attempts_status'))
        batch_op.drop_index(batch_op.f('ix_send_attempts_ref_id'))
        batch_op.drop_index(batch_op.f('ix_send_attempts_kind'))
        batch_op.drop_index(batch_op.f('ix_send_attempts_client_id'))

    op.drop_table('send_attempts')
    with op.batch_alter_table('response_automation_settings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_response_automation_settings_client_id'))

    op.drop_table('response_automation_settings')
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notifications_user_id'))
        batch_op.drop_index(batch_op.f('ix_notifications_client_id'))

    op.drop_table('notifications')
    with op.batch_alter_table('notification_preferences', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_preferences_client_id'))

    op.drop_table('notification_preferences')
    with op.batch_alter_table('inbound_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_inbound_events_client_id'))

    op.drop_table('inbound_events')
    with op.batch_alter_table('faqs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_faqs_client_id'))

    op.drop_table('faqs')
    with op.batch_alter_table('evaluation_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_evaluation_runs_client_id'))

    op.drop_table('evaluation_runs')
    with op.batch_alter_table('customer_contacts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_contacts_client_id'))

    op.drop_table('customer_contacts')
    with op.batch_alter_table('contact_suppressions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contact_suppressions_client_id'))

    op.drop_table('contact_suppressions')
    with op.batch_alter_table('approved_templates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_approved_templates_client_id'))

    op.drop_table('approved_templates')
    op.drop_table('pilot_recipients')
    with op.batch_alter_table('sandbox_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sandbox_records_kind'))

    op.drop_table('sandbox_records')
    op.drop_table('rate_counters')

    with op.batch_alter_table('vehicles', schema=None) as batch_op:
        batch_op.drop_column('data_conflict_note')
        batch_op.drop_column('verified_at')

    with op.batch_alter_table('integration_connections', schema=None) as batch_op:
        batch_op.drop_column('config')
        batch_op.drop_column('external_account_id')
        batch_op.drop_column('paused')

    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.drop_constraint('fk_clients_business_info_approved_by', type_='foreignkey')
        batch_op.drop_constraint('uq_clients_public_chat_key', type_='unique')
        batch_op.drop_column('business_info_approved_by_id')
        batch_op.drop_column('business_info_approved_at')
        batch_op.drop_column('business_info_approved')
        batch_op.drop_column('public_chat_key')

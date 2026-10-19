"""Phase 3: customer conversations, drafts, escalations, appointments, response automation, notifications.

Replaces the interim Phase 4 tables without losing their rows:
1. the interim ``conversations``, ``conversation_messages`` and ``appointments`` are renamed to ``legacy_*``
   (their named indexes, and on PostgreSQL their primary-key indexes and sequences, are renamed with them);
2. the Phase 3 tables are created;
3. ``dealerflow.legacy_conversations.convert`` copies the legacy rows across (idempotent; re-runnable with
   ``python -m dealerflow convert-legacy-conversations``).
The ``legacy_*`` tables are kept as a read-only archive. Removing them is a separate, later migration once the
converted data has been checked (docs/architecture/phase3-integration.md).

Downgrade drops the Phase 3 tables (losing data created since the upgrade) and renames the legacy tables back.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

LEGACY = ("conversations", "conversation_messages", "appointments")
LEGACY_INDEXES = {
    "conversations": ["ix_conversations_client_id"],
    "conversation_messages": ["ix_conversation_messages_client_id", "ix_conversation_messages_conversation_id"],
    "appointments": ["ix_appointments_client_id"],
}


def _pg_rename_implicit(prefix_from: str, prefix_to: str) -> None:
    """PostgreSQL keeps primary-key/unique index and sequence names after a table rename; move them too."""
    if op.get_bind().dialect.name != "postgresql":
        return
    renames = [("conversations_pkey", "INDEX"), ("conversation_messages_pkey", "INDEX"), ("appointments_pkey", "INDEX"),
               ("conversation_messages_provider_message_id_key", "INDEX"),
               ("conversations_id_seq", "SEQUENCE"), ("conversation_messages_id_seq", "SEQUENCE"), ("appointments_id_seq", "SEQUENCE")]
    for name, kind in renames:
        op.execute(f"ALTER {kind} IF EXISTS {prefix_from}{name} RENAME TO {prefix_to}{name}")


def upgrade() -> None:
    for table in LEGACY:
        for ix in LEGACY_INDEXES[table]:
            op.drop_index(ix, table_name=table)
        op.rename_table(table, f"legacy_{table}")
    _pg_rename_implicit("", "legacy_")

    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('business_info_approved_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('business_info_approved_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('public_chat_key', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('adf_intake_key', sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint('uq_clients_public_chat_key', ['public_chat_key'])
        batch_op.create_unique_constraint('uq_clients_adf_intake_key', ['adf_intake_key'])
        batch_op.create_foreign_key('fk_clients_business_info_approved_by', 'users', ['business_info_approved_by_id'], ['id'])

    op.create_table('rate_counters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('window_start', sa.DateTime(), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key', 'window_start', name='uq_rate_counters_window')
    )

    op.create_table('customer_contacts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=True),
    sa.Column('email', sa.String(length=200), nullable=True),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('preferred_language', sa.String(length=5), nullable=True),
    sa.Column('contact_source', sa.String(length=100), nullable=True),
    sa.Column('opted_out_at', sa.DateTime(), nullable=True),
    sa.Column('simulated', sa.Boolean(), nullable=False),
    sa.Column('pii_purged_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'id', name='uq_customer_contacts_client_id')
    )
    with op.batch_alter_table('customer_contacts', schema=None) as batch_op:
        batch_op.create_index('ix_customer_contacts_client_email', ['client_id', 'email'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_contacts_client_id'), ['client_id'], unique=False)

    op.create_table('customer_suppressions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('address', sa.String(length=200), nullable=False),
    sa.Column('reason', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'channel', 'address', name='uq_customer_suppressions')
    )
    with op.batch_alter_table('customer_suppressions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_suppressions_client_id'), ['client_id'], unique=False)

    op.create_table('dealer_faqs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('language', sa.String(length=5), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('answer', sa.Text(), nullable=False),
    sa.Column('keywords', sa.JSON(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('auto_send_allowed', sa.Boolean(), nullable=False),
    sa.Column('approved_by_id', sa.Integer(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['approved_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('dealer_faqs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_dealer_faqs_client_id'), ['client_id'], unique=False)

    op.create_table('response_automation_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=False),
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
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['paused_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', name='uq_response_automation_settings_client')
    )
    with op.batch_alter_table('response_automation_settings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_response_automation_settings_client_id'), ['client_id'], unique=False)

    op.create_table('response_evaluation_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=True),
    sa.Column('engine_version', sa.String(length=20), nullable=False),
    sa.Column('total_cases', sa.Integer(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('failures', sa.JSON(), nullable=False),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('triggered_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['triggered_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('response_evaluation_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_response_evaluation_runs_client_id'), ['client_id'], unique=False)

    op.create_table('response_templates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('intent', sa.String(length=40), nullable=False),
    sa.Column('language', sa.String(length=5), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('auto_send_allowed', sa.Boolean(), nullable=False),
    sa.Column('approved_by_id', sa.Integer(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['approved_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('response_templates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_response_templates_client_id'), ['client_id'], unique=False)

    op.create_table('staff_notifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('channel', sa.String(length=10), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('subject', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('link', sa.String(length=400), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('status_detail', sa.Text(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('read_at', sa.DateTime(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    with op.batch_alter_table('staff_notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_staff_notifications_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_staff_notifications_user_id'), ['user_id'], unique=False)

    op.create_table('customer_identities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id', 'contact_id'], ['customer_contacts.client_id', 'customer_contacts.id'], name='fk_customer_identities_contact', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'channel', 'external_id', name='uq_customer_identities_external')
    )
    with op.batch_alter_table('customer_identities', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_identities_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_identities_contact_id'), ['contact_id'], unique=False)

    op.create_table('response_mode_authorizations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('authorized_by_id', sa.Integer(), nullable=False),
    sa.Column('evaluation_run_id', sa.Integer(), nullable=False),
    sa.Column('authorized_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_by_id', sa.Integer(), nullable=True),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['authorized_by_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['evaluation_run_id'], ['response_evaluation_runs.id'], ),
    sa.ForeignKeyConstraint(['revoked_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('response_mode_authorizations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_response_mode_authorizations_client_id'), ['client_id'], unique=False)

    op.create_table('customer_leads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=False),
    sa.Column('vehicle_id', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('assigned_user_id', sa.Integer(), nullable=True),
    sa.Column('external_ref', sa.String(length=200), nullable=True),
    sa.Column('simulated', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
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

    op.create_table('conversations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('external_conversation_id', sa.String(length=200), nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=True),
    sa.Column('vehicle_id', sa.Integer(), nullable=True),
    sa.Column('lead_id', sa.Integer(), nullable=True),
    sa.Column('preferred_language', sa.String(length=5), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('assigned_user_id', sa.Integer(), nullable=True),
    sa.Column('human_takeover', sa.Boolean(), nullable=False),
    sa.Column('takeover_by_id', sa.Integer(), nullable=True),
    sa.Column('takeover_at', sa.DateTime(), nullable=True),
    sa.Column('opted_out', sa.Boolean(), nullable=False),
    sa.Column('opted_out_at', sa.DateTime(), nullable=True),
    sa.Column('human_requested', sa.Boolean(), nullable=False),
    sa.Column('lead_qualified', sa.Boolean(), nullable=False),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('summary_updated_at', sa.DateTime(), nullable=True),
    sa.Column('summary_delivered_at', sa.DateTime(), nullable=True),
    sa.Column('unresolved_questions', sa.JSON(), nullable=False),
    sa.Column('next_action', sa.Text(), nullable=True),
    sa.Column('appointment_status', sa.String(length=30), nullable=False),
    sa.Column('escalation_status', sa.String(length=20), nullable=False),
    sa.Column('simulated', sa.Boolean(), nullable=False),
    sa.Column('first_inbound_at', sa.DateTime(), nullable=True),
    sa.Column('first_response_at', sa.DateTime(), nullable=True),
    sa.Column('last_inbound_at', sa.DateTime(), nullable=True),
    sa.Column('last_outbound_at', sa.DateTime(), nullable=True),
    sa.Column('pii_purged_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id', 'contact_id'], ['customer_contacts.client_id', 'customer_contacts.id'], name='fk_conversations_contact'),
    sa.ForeignKeyConstraint(['client_id', 'lead_id'], ['customer_leads.client_id', 'customer_leads.id'], name='fk_conversations_lead'),
    sa.ForeignKeyConstraint(['client_id', 'vehicle_id'], ['vehicles.client_id', 'vehicles.id'], name='fk_conversations_vehicle'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['takeover_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'channel', 'external_conversation_id', name='uq_conversations_external'),
    sa.UniqueConstraint('client_id', 'id', name='uq_conversations_client_id')
    )
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversations_client_id'), ['client_id'], unique=False)
        batch_op.create_index('ix_conversations_client_status', ['client_id', 'status'], unique=False)

    op.create_table('appointments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=True),
    sa.Column('contact_id', sa.Integer(), nullable=True),
    sa.Column('vehicle_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('customer_name', sa.String(length=200), nullable=True),
    sa.Column('customer_phone', sa.String(length=40), nullable=True),
    sa.Column('customer_email', sa.String(length=200), nullable=True),
    sa.Column('requested_at', sa.DateTime(), nullable=False),
    sa.Column('requested_start', sa.DateTime(), nullable=True),
    sa.Column('duration_minutes', sa.Integer(), nullable=False),
    sa.Column('confirmed_start', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('assigned_user_id', sa.Integer(), nullable=True),
    sa.Column('availability_checked', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.CheckConstraint("status in ('requested','pending_confirmation','confirmed','declined','cancelled','reschedule_requested','completed','no_show')", name='ck_appointments_status_v2'),
    sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id', 'contact_id'], ['customer_contacts.client_id', 'customer_contacts.id'], name='fk_appointments_contact'),
    sa.ForeignKeyConstraint(['client_id', 'conversation_id'], ['conversations.client_id', 'conversations.id'], name='fk_appointments_conversation'),
    sa.ForeignKeyConstraint(['client_id', 'vehicle_id'], ['vehicles.client_id', 'vehicles.id'], name='fk_appointments_vehicle'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'id', name='uq_appointments_client_id')
    )
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_appointments_client_id'), ['client_id'], unique=False)

    op.create_table('conversation_escalations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('reasons', sa.JSON(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('recommended_action', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('assigned_user_id', sa.Integer(), nullable=True),
    sa.Column('resolved_by_id', sa.Integer(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['client_id', 'conversation_id'], ['conversations.client_id', 'conversations.id'], name='fk_conversation_escalations_conversation', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['resolved_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('conversation_escalations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversation_escalations_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_escalations_conversation_id'), ['conversation_id'], unique=False)

    op.create_table('conversation_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('sender_type', sa.String(length=20), nullable=False),
    sa.Column('sender_user_id', sa.Integer(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=5), nullable=True),
    sa.Column('provider_message_id', sa.String(length=300), nullable=True),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('delivery_status', sa.String(length=20), nullable=False),
    sa.Column('delivery_error', sa.Text(), nullable=True),
    sa.Column('sending_started_at', sa.DateTime(), nullable=True),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=200), nullable=True),
    sa.Column('analysis', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id', 'conversation_id'], ['conversations.client_id', 'conversations.id'], name='fk_conversation_messages_conversation', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['sender_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'channel', 'provider_message_id', name='uq_conversation_messages_provider'),
    sa.UniqueConstraint('client_id', 'id', name='uq_conversation_messages_client_id'),
    sa.UniqueConstraint('idempotency_key')
    )
    with op.batch_alter_table('conversation_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversation_messages_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_messages_conversation_id'), ['conversation_id'], unique=False)

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
    sa.Column('decision', sa.String(length=20), nullable=True),
    sa.Column('decision_reasons', sa.JSON(), nullable=False),
    sa.Column('generated_by', sa.String(length=40), nullable=False),
    sa.Column('engine_version', sa.String(length=20), nullable=False),
    sa.Column('reviewed_by_id', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('sent_message_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id', 'conversation_id'], ['conversations.client_id', 'conversations.id'], name='fk_response_drafts_conversation', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.ForeignKeyConstraint(['reviewed_by_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'id', name='uq_response_drafts_client_id')
    )
    with op.batch_alter_table('response_drafts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_response_drafts_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_response_drafts_conversation_id'), ['conversation_id'], unique=False)

    op.create_table('appointment_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('appointment_id', sa.Integer(), nullable=False),
    sa.Column('event', sa.String(length=40), nullable=False),
    sa.Column('actor', sa.String(length=80), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('client_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id', 'appointment_id'], ['appointments.client_id', 'appointments.id'], name='fk_appointment_events_appointment', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('appointment_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_appointment_events_appointment_id'), ['appointment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_appointment_events_client_id'), ['client_id'], unique=False)

    from dealerflow.legacy_conversations import convert

    convert(op.get_bind())


def downgrade() -> None:
    op.drop_table('appointment_events')
    op.drop_table('response_drafts')
    op.drop_table('conversation_messages')
    op.drop_table('conversation_escalations')
    op.drop_table('appointments')
    op.drop_table('conversations')
    op.drop_table('customer_leads')
    op.drop_table('response_mode_authorizations')
    op.drop_table('customer_identities')
    op.drop_table('staff_notifications')
    op.drop_table('response_templates')
    op.drop_table('response_evaluation_runs')
    op.drop_table('response_automation_settings')
    op.drop_table('dealer_faqs')
    op.drop_table('customer_suppressions')
    op.drop_table('customer_contacts')
    op.drop_table('rate_counters')
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.drop_constraint('fk_clients_business_info_approved_by', type_='foreignkey')
        batch_op.drop_constraint('uq_clients_adf_intake_key', type_='unique')
        batch_op.drop_constraint('uq_clients_public_chat_key', type_='unique')
        batch_op.drop_column('adf_intake_key')
        batch_op.drop_column('public_chat_key')
        batch_op.drop_column('business_info_approved_by_id')
        batch_op.drop_column('business_info_approved_at')

    _pg_rename_implicit("legacy_", "")
    for table in LEGACY:
        op.rename_table(f"legacy_{table}", table)
        for ix in LEGACY_INDEXES[table]:
            cols = {"ix_conversations_client_id": ["client_id"], "ix_conversation_messages_client_id": ["client_id"],
                    "ix_conversation_messages_conversation_id": ["conversation_id"], "ix_appointments_client_id": ["client_id"]}[ix]
            op.create_index(ix, table, cols, unique=False)

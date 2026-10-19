"""INTERIM Phase 3 tables (conversations, conversation_messages, appointments).

Stand-ins used by Phase 4 reporting and the end-to-end test until Phase 3 is ported into dealerflow.
Phase 3 should add a migration after this one that drops or converts these tables
(docs/architecture/shared-stack.md).

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('conversations',
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
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversations_client_id'), ['client_id'], unique=False)

    op.create_table('appointments',
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
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_appointments_client_id'), ['client_id'], unique=False)

    op.create_table('conversation_messages',
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
    sa.UniqueConstraint('provider_message_id')
    )
    with op.batch_alter_table('conversation_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversation_messages_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_messages_conversation_id'), ['conversation_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('conversation_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conversation_messages_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_conversation_messages_client_id'))

    op.drop_table('conversation_messages')
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_appointments_client_id'))

    op.drop_table('appointments')
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conversations_client_id'))

    op.drop_table('conversations')

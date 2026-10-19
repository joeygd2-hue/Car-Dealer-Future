"""Per-report manager approval before a client report can be emailed (decision D4).

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('client_reports') as batch:
        batch.add_column(sa.Column('approved_by_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('approved_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('approved_digest', sa.String(length=64), nullable=True))
        batch.create_foreign_key('fk_client_reports_approved_by_id', 'users', ['approved_by_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('client_reports') as batch:
        batch.drop_constraint('fk_client_reports_approved_by_id', type_='foreignkey')
        batch.drop_column('approved_digest')
        batch.drop_column('approved_at')
        batch.drop_column('approved_by_id')

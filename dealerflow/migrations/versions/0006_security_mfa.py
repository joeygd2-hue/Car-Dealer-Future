"""Security: TOTP MFA columns on users (ported from Phase 3). Additive; existing users get MFA off.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mfa_enabled', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('mfa_secret_encrypted', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('mfa_pending_secret_encrypted', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('mfa_last_counter', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('mfa_recovery_hashes', sa.JSON(), server_default=sa.text("'[]'"), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('mfa_recovery_hashes')
        batch_op.drop_column('mfa_last_counter')
        batch_op.drop_column('mfa_pending_secret_encrypted')
        batch_op.drop_column('mfa_secret_encrypted')
        batch_op.drop_column('mfa_enabled')

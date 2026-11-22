"""Tidy: drop pairing codes and unused device columns; neutral settings keys

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table('pairing_codes')
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_index('ix_devices_cert_fingerprint')
        batch_op.drop_column('cert_fingerprint')
        batch_op.drop_column('public_key_jwk')
    op.execute("UPDATE app_settings SET key = 'settings:' || substr(key, 4) WHERE key LIKE 'v2:%'")


def downgrade() -> None:
    op.execute("UPDATE app_settings SET key = 'v2:' || substr(key, 10) WHERE key LIKE 'settings:%'")
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('public_key_jwk', sa.Text(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('cert_fingerprint', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_devices_cert_fingerprint', ['cert_fingerprint'], unique=False)
    op.create_table('pairing_codes',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('code_hash', sa.String(length=64), nullable=False),
                    sa.Column('device_name', sa.String(length=64), nullable=True),
                    sa.Column('cert_fingerprint', sa.String(length=64), nullable=True),
                    sa.Column('created_by', sa.String(length=32), nullable=False),
                    sa.Column('created_at', sa.DateTime(), nullable=False),
                    sa.Column('expires_at', sa.DateTime(), nullable=False),
                    sa.Column('used_at', sa.DateTime(), nullable=True),
                    sa.Column('attempts', sa.Integer(), nullable=False),
                    sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('code_hash'))

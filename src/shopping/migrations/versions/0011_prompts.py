"""Prompts edited in the app, with revisions

Revision ID: 0011
Revises: 0010
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('prompt_overrides',
                    sa.Column('prompt_id', sa.String(length=64), nullable=False),
                    sa.Column('body', sa.Text(), nullable=False),
                    sa.Column('tier', sa.String(length=8), nullable=True),
                    sa.Column('updated_at', sa.DateTime(), nullable=False),
                    sa.Column('updated_by', sa.String(length=64), nullable=True),
                    sa.PrimaryKeyConstraint('prompt_id'))
    op.create_table('prompt_revisions',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('prompt_id', sa.String(length=64), nullable=False),
                    sa.Column('body', sa.Text(), nullable=False),
                    sa.Column('tier', sa.String(length=8), nullable=True),
                    sa.Column('saved_at', sa.DateTime(), nullable=False),
                    sa.Column('saved_by', sa.String(length=64), nullable=True),
                    sa.Column('note', sa.String(length=32), nullable=True),
                    sa.PrimaryKeyConstraint('id'))
    with op.batch_alter_table('prompt_revisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_prompt_revisions_prompt_id'), ['prompt_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('prompt_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_prompt_revisions_prompt_id'))
    op.drop_table('prompt_revisions')
    op.drop_table('prompt_overrides')

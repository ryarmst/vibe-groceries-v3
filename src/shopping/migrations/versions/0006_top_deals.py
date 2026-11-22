"""top deals

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'top_deals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('price_observation_id', sa.Integer(), nullable=True),
        sa.Column('headline', sa.String(length=128), nullable=False),
        sa.Column('why', sa.Text(), nullable=True),
        sa.Column('relevant_to', sa.String(length=128), nullable=True),
        sa.Column('rating', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('suggested_quantity', sa.Float(), nullable=False, server_default='1'),
        sa.Column('added_by', sa.String(length=16), nullable=False, server_default='agent'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='active'),
        sa.Column('acted_at', sa.DateTime(), nullable=True),
        sa.Column('response_note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['price_observation_id'], ['price_observations.id']),
        sa.ForeignKeyConstraint(['product_id'], ['products.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('top_deals', schema=None) as batch_op:
        batch_op.create_index('ix_top_deals_created_at', ['created_at'])
        batch_op.create_index('ix_top_deals_product_id', ['product_id'])
        batch_op.create_index('ix_top_deals_relevant_to', ['relevant_to'])
        batch_op.create_index('ix_top_deals_status', ['status'])


def downgrade() -> None:
    op.drop_table('top_deals')

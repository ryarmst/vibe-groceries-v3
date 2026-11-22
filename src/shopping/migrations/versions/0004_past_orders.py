"""past orders

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16 20:43:59.340621

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    
    op.create_table('past_orders',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('placed_at', sa.DateTime(), nullable=True),
    sa.Column('store', sa.String(length=128), nullable=True),
    sa.Column('fulfillment', sa.String(length=32), nullable=True),
    sa.Column('subtotal', sa.String(length=40), nullable=True),
    sa.Column('tax', sa.String(length=40), nullable=True),
    sa.Column('total', sa.String(length=40), nullable=True),
    sa.Column('points_earned', sa.Float(), nullable=True),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('past_orders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_past_orders_placed_at'), ['placed_at'], unique=False)

    op.create_table('past_order_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.String(length=32), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('retailer_product_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('brand', sa.String(length=128), nullable=True),
    sa.Column('size', sa.String(length=64), nullable=True),
    sa.Column('quantity', sa.Float(), nullable=True),
    sa.Column('weight_kg', sa.Float(), nullable=True),
    sa.Column('unit_price', sa.String(length=40), nullable=True),
    sa.Column('total', sa.String(length=40), nullable=True),
    sa.Column('fulfilled', sa.Boolean(), nullable=False),
    sa.Column('is_fee', sa.Boolean(), nullable=False),
    sa.Column('selection_id', sa.String(length=16), nullable=True),
    sa.ForeignKeyConstraint(['order_id'], ['past_orders.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['selection_id'], ['selections.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('past_order_lines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_past_order_lines_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_past_order_lines_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_past_order_lines_retailer_product_id'), ['retailer_product_id'], unique=False)

    


def downgrade() -> None:
    """Downgrade schema."""
    
    with op.batch_alter_table('past_order_lines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_past_order_lines_retailer_product_id'))
        batch_op.drop_index(batch_op.f('ix_past_order_lines_product_id'))
        batch_op.drop_index(batch_op.f('ix_past_order_lines_order_id'))

    op.drop_table('past_order_lines')
    with op.batch_alter_table('past_orders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_past_orders_placed_at'))

    op.drop_table('past_orders')
    

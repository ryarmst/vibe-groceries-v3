"""history details

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03 15:18:39.511874

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    
    with op.batch_alter_table('cart_actions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('product_label', sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column('source', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_cart_actions_timestamp'), ['timestamp'], unique=False)

    with op.batch_alter_table('selections', schema=None) as batch_op:
        batch_op.add_column(sa.Column('suggested_quantity', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('added_by', sa.String(length=16), nullable=False, server_default='agent'))

    op.execute("UPDATE selections SET suggested_quantity = quantity WHERE suggested_quantity IS NULL")
    op.execute("UPDATE cart_actions SET source = 'apply' WHERE source IS NULL AND shopping_run_id IS NOT NULL")
    op.execute("UPDATE cart_actions SET source = 'cart_page' WHERE source IS NULL")
    op.execute(
        "UPDATE cart_actions SET product_label = (SELECT trim(coalesce(p.brand || ' ', '') || p.name || coalesce(' ' || p.raw_size, '')) "
        "FROM products p WHERE p.id = cart_actions.product_id) WHERE product_label IS NULL"
    )

    


def downgrade() -> None:
    """Downgrade schema."""
    
    with op.batch_alter_table('selections', schema=None) as batch_op:
        batch_op.drop_column('added_by')
        batch_op.drop_column('suggested_quantity')

    with op.batch_alter_table('cart_actions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cart_actions_timestamp'))
        batch_op.drop_column('source')
        batch_op.drop_column('product_label')

    

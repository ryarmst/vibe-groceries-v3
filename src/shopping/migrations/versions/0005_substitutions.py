"""substitutions

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16 21:02:06.751485

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    
    with op.batch_alter_table('past_order_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('substitution_allowed', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('substituted_with', sa.String(length=256), nullable=True))

    with op.batch_alter_table('selections', schema=None) as batch_op:
        batch_op.add_column(sa.Column('substitution_allowed', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('substitution_note', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('substitute_product_id', sa.String(length=64), nullable=True))

    


def downgrade() -> None:
    """Downgrade schema."""
    
    with op.batch_alter_table('selections', schema=None) as batch_op:
        batch_op.drop_column('substitute_product_id')
        batch_op.drop_column('substitution_note')
        batch_op.drop_column('substitution_allowed')

    with op.batch_alter_table('past_order_lines', schema=None) as batch_op:
        batch_op.drop_column('substituted_with')
        batch_op.drop_column('substitution_allowed')

    

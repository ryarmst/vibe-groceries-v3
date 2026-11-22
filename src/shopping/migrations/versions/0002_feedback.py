"""feedback

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03 13:27:48.911900

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    
    op.create_table('feedback',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('shopping_run_id', sa.String(length=16), nullable=True),
    sa.Column('selection_id', sa.String(length=16), nullable=True),
    sa.Column('choice_group_id', sa.String(length=16), nullable=True),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('chosen_product_id', sa.Integer(), nullable=True),
    sa.Column('requirement', sa.String(length=128), nullable=True),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('tags', sa.Text(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('context', sa.Text(), nullable=True),
    sa.Column('preference_id', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('resolution', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['chosen_product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['preference_id'], ['preferences.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['selection_id'], ['selections.id'], ),
    sa.ForeignKeyConstraint(['shopping_run_id'], ['shopping_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_feedback_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_feedback_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_feedback_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_feedback_requirement'), ['requirement'], unique=False)
        batch_op.create_index(batch_op.f('ix_feedback_selection_id'), ['selection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_feedback_shopping_run_id'), ['shopping_run_id'], unique=False)

    


def downgrade() -> None:
    """Downgrade schema."""
    
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_feedback_shopping_run_id'))
        batch_op.drop_index(batch_op.f('ix_feedback_selection_id'))
        batch_op.drop_index(batch_op.f('ix_feedback_requirement'))
        batch_op.drop_index(batch_op.f('ix_feedback_product_id'))
        batch_op.drop_index(batch_op.f('ix_feedback_created_at'))
        batch_op.drop_index(batch_op.f('ix_feedback_action'))

    op.drop_table('feedback')
    

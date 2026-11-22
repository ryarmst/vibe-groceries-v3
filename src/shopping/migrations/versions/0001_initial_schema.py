"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-03 12:48:41.462941

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('preferences',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('scope', sa.String(length=128), nullable=True),
    sa.Column('target', sa.String(length=128), nullable=True),
    sa.Column('target_label', sa.String(length=256), nullable=True),
    sa.Column('category', sa.String(length=64), nullable=True),
    sa.Column('weight', sa.Float(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('source_selection_id', sa.String(length=16), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('preferences', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_preferences_active'), ['active'], unique=False)
        batch_op.create_index(batch_op.f('ix_preferences_kind'), ['kind'], unique=False)

    op.create_table('products',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('retailer_product_id', sa.String(length=64), nullable=False),
    sa.Column('article_number', sa.String(length=32), nullable=True),
    sa.Column('uom', sa.String(length=16), nullable=True),
    sa.Column('gtin', sa.String(length=128), nullable=True),
    sa.Column('brand', sa.String(length=128), nullable=True),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('raw_size', sa.String(length=64), nullable=True),
    sa.Column('raw_package_sizing', sa.String(length=128), nullable=True),
    sa.Column('size_value', sa.String(length=40), nullable=True),
    sa.Column('size_unit', sa.String(length=8), nullable=True),
    sa.Column('size_estimated', sa.Boolean(), nullable=False),
    sa.Column('comparison_family', sa.String(length=16), nullable=False),
    sa.Column('sold_by', sa.String(length=32), nullable=False),
    sa.Column('image_url', sa.String(length=512), nullable=True),
    sa.Column('link', sa.String(length=512), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_products_brand'), ['brand'], unique=False)
        batch_op.create_index(batch_op.f('ix_products_retailer_product_id'), ['retailer_product_id'], unique=True)

    op.create_table('shopping_runs',
    sa.Column('id', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('store_id', sa.String(length=16), nullable=True),
    sa.Column('summary', sa.String(length=256), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('choice_groups',
    sa.Column('id', sa.String(length=16), nullable=False),
    sa.Column('shopping_run_id', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('label', sa.String(length=128), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('recommended_selection_id', sa.String(length=16), nullable=True),
    sa.Column('selected_selection_id', sa.String(length=16), nullable=True),
    sa.ForeignKeyConstraint(['shopping_run_id'], ['shopping_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('choice_groups', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_choice_groups_shopping_run_id'), ['shopping_run_id'], unique=False)

    op.create_table('price_observations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('shopping_run_id', sa.String(length=16), nullable=True),
    sa.Column('observed_at', sa.DateTime(), nullable=False),
    sa.Column('store_id', sa.String(length=16), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('regular_price', sa.String(length=40), nullable=True),
    sa.Column('sale_price', sa.String(length=40), nullable=True),
    sa.Column('effective_price', sa.String(length=40), nullable=True),
    sa.Column('member_price', sa.String(length=40), nullable=True),
    sa.Column('raw_package_size', sa.String(length=64), nullable=True),
    sa.Column('raw_package_sizing', sa.String(length=128), nullable=True),
    sa.Column('normalized_quantity', sa.String(length=40), nullable=True),
    sa.Column('normalized_unit', sa.String(length=8), nullable=True),
    sa.Column('size_estimated', sa.Boolean(), nullable=False),
    sa.Column('unit_price', sa.String(length=40), nullable=True),
    sa.Column('unit_price_basis', sa.String(length=16), nullable=True),
    sa.Column('retailer_unit_price', sa.String(length=64), nullable=True),
    sa.Column('deal_type', sa.String(length=32), nullable=True),
    sa.Column('deal_text', sa.String(length=128), nullable=True),
    sa.Column('deal_expiry', sa.String(length=32), nullable=True),
    sa.Column('pc_optimum_text', sa.String(length=128), nullable=True),
    sa.Column('promotion_text', sa.String(length=256), nullable=True),
    sa.Column('availability', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['shopping_run_id'], ['shopping_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('price_observations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_price_observations_observed_at'), ['observed_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_price_observations_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_price_observations_shopping_run_id'), ['shopping_run_id'], unique=False)

    op.create_table('selections',
    sa.Column('id', sa.String(length=16), nullable=False),
    sa.Column('shopping_run_id', sa.String(length=16), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('choice_group_id', sa.String(length=16), nullable=True),
    sa.Column('price_observation_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('quantity', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('requirement', sa.String(length=128), nullable=True),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('reason_codes', sa.Text(), nullable=False),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('rejected_at', sa.DateTime(), nullable=True),
    sa.Column('applied_to_cart_at', sa.DateTime(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['choice_group_id'], ['choice_groups.id'], ),
    sa.ForeignKeyConstraint(['price_observation_id'], ['price_observations.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['shopping_run_id'], ['shopping_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('selections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_selections_choice_group_id'), ['choice_group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_selections_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_selections_shopping_run_id'), ['shopping_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_selections_status'), ['status'], unique=False)

    op.create_table('cart_actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shopping_run_id', sa.String(length=16), nullable=True),
    sa.Column('selection_id', sa.String(length=16), nullable=True),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('retailer_product_id', sa.String(length=64), nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('quantity_before', sa.Float(), nullable=True),
    sa.Column('quantity_after', sa.Float(), nullable=True),
    sa.Column('price_at_action', sa.String(length=40), nullable=True),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('upstream_reference', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['selection_id'], ['selections.id'], ),
    sa.ForeignKeyConstraint(['shopping_run_id'], ['shopping_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('cart_actions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cart_actions_shopping_run_id'), ['shopping_run_id'], unique=False)

    


def downgrade() -> None:
    """Downgrade schema."""
    
    with op.batch_alter_table('cart_actions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cart_actions_shopping_run_id'))

    op.drop_table('cart_actions')
    with op.batch_alter_table('selections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_selections_status'))
        batch_op.drop_index(batch_op.f('ix_selections_shopping_run_id'))
        batch_op.drop_index(batch_op.f('ix_selections_product_id'))
        batch_op.drop_index(batch_op.f('ix_selections_choice_group_id'))

    op.drop_table('selections')
    with op.batch_alter_table('price_observations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_price_observations_shopping_run_id'))
        batch_op.drop_index(batch_op.f('ix_price_observations_product_id'))
        batch_op.drop_index(batch_op.f('ix_price_observations_observed_at'))

    op.drop_table('price_observations')
    with op.batch_alter_table('choice_groups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_choice_groups_shopping_run_id'))

    op.drop_table('choice_groups')
    op.drop_table('shopping_runs')
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_products_retailer_product_id'))
        batch_op.drop_index(batch_op.f('ix_products_brand'))

    op.drop_table('products')
    with op.batch_alter_table('preferences', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_preferences_kind'))
        batch_op.drop_index(batch_op.f('ix_preferences_active'))

    op.drop_table('preferences')
    op.drop_table('app_settings')
    

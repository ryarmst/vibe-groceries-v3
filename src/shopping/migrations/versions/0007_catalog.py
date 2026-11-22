"""catalogue: categories, current prices on products, deal scans

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-20 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('category_code', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('category_path', sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column('aisle', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('current_price', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('current_regular_price', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('current_unit_price', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('current_unit_basis', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('current_deal_text', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('current_deal_expiry', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('current_on_sale', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('current_availability', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('current_observed_at', sa.DateTime(), nullable=True))
        batch_op.create_index('ix_products_category_code', ['category_code'])
        batch_op.create_index('ix_products_aisle', ['aisle'])
        batch_op.create_index('ix_products_current_on_sale', ['current_on_sale'])
        batch_op.create_index('ix_products_current_observed_at', ['current_observed_at'])

    op.create_table(
        'categories',
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('parent_code', sa.String(length=16), nullable=True),
        sa.Column('level', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('path', sa.String(length=256), nullable=False),
        sa.PrimaryKeyConstraint('code'),
    )
    op.create_index('ix_categories_parent_code', 'categories', ['parent_code'])
    op.create_table(
        'deal_scans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='running'),
        sa.Column('pages', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('products_seen', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_deal_scans_started_at', 'deal_scans', ['started_at'])

    # Backfill current_* from each product's newest non-order observation.
    op.execute("""
        UPDATE products SET
          current_price = o.effective_price, current_regular_price = o.regular_price,
          current_unit_price = o.unit_price, current_unit_basis = o.unit_price_basis,
          current_deal_text = o.deal_text, current_deal_expiry = substr(o.deal_expiry, 1, 10),
          current_on_sale = CASE WHEN o.deal_text IS NOT NULL OR (o.sale_price IS NOT NULL AND o.sale_price != o.regular_price) THEN 1 ELSE 0 END,
          current_availability = o.availability, current_observed_at = o.observed_at
        FROM (SELECT po.* FROM price_observations po
              WHERE po.source != 'order' AND po.id = (SELECT p2.id FROM price_observations p2
                   WHERE p2.product_id = po.product_id AND p2.source != 'order' ORDER BY p2.observed_at DESC LIMIT 1)) AS o
        WHERE o.product_id = products.id
    """)


def downgrade() -> None:
    op.drop_table('deal_scans')
    op.drop_table('categories')
    with op.batch_alter_table('products', schema=None) as batch_op:
        for ix in ('ix_products_current_observed_at', 'ix_products_current_on_sale', 'ix_products_aisle', 'ix_products_category_code'):
            batch_op.drop_index(ix)
        for col in ('current_observed_at', 'current_availability', 'current_on_sale', 'current_deal_expiry', 'current_deal_text',
                    'current_unit_basis', 'current_unit_price', 'current_regular_price', 'current_price', 'aisle', 'category_path',
                    'category_code'):
            batch_op.drop_column(col)

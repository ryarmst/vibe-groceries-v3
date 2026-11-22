"""Web access: drop the old browser-supplied retailer token and pairing codes

People now come from Cloudflare Access (rows in `devices` keyed by a hash of the email), and the No Frills
session is PC ID refresh tokens in DATA_DIR/pcid.

Revision ID: 0009
Revises: 0008
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM secrets WHERE name = 'retailer:access_token'")
    op.execute("DELETE FROM pairing_codes")


def downgrade() -> None:
    pass

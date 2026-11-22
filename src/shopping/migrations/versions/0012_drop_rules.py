"""Remove the old rules system and reason tags; devices -> users

The household's preferences live in Notion (Shopping Preferences), and review notes are free text,
so the `preferences` table, feedback tags and the link between them go. `devices` holds people now.

Revision ID: 0012
Revises: 0011
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('feedback', schema=None, recreate='always') as batch_op:
        batch_op.drop_column('preference_id')
        batch_op.drop_column('tags')
    with op.batch_alter_table('preferences', schema=None) as batch_op:
        batch_op.drop_index('ix_preferences_active')
        batch_op.drop_index('ix_preferences_kind')
    op.drop_table('preferences')
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('revoked_at')
    op.rename_table('devices', 'users')
    # The {device_name} prompt placeholder is now {person}.
    op.execute("UPDATE prompt_overrides SET body = replace(body, '{device_name}', '{person}')")
    op.execute("UPDATE prompt_revisions SET body = replace(body, '{device_name}', '{person}')")


def downgrade() -> None:
    raise NotImplementedError("Restore the database backup taken before upgrading instead.")

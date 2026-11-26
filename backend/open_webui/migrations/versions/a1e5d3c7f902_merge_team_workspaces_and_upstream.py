"""Merge the team workspace/quota branch with upstream's chat_message, resource_tag and machine migrations.

Both branches start from 9f6c2a8d1b43 and touch disjoint tables, so either order is valid.
"""

revision = 'a1e5d3c7f902'
down_revision = ('5e7a2c9d4b13', 'c4a7e2b9d531')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass

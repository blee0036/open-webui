"""Add per-package storage limits: uploaded files and sandbox home volumes (NULL = unlimited)."""

import sqlalchemy as sa
from alembic import op

revision = 'e3b9a6c1d742'
down_revision = 'a1e5d3c7f902'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('quota_plan') as batch:
        batch.add_column(sa.Column('storage_bytes', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('sandbox_bytes', sa.BigInteger(), nullable=True))


def downgrade():
    with op.batch_alter_table('quota_plan') as batch:
        batch.drop_column('sandbox_bytes')
        batch.drop_column('storage_bytes')

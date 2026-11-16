"""Add enterprise identities, independent package accounting and project boundaries."""

import sqlalchemy as sa
from alembic import op

revision = '30e4b65c927a'
down_revision = '9f6c2a8d1b43'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'enterprise_connection',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('platform', sa.Text(), nullable=False),
        sa.Column('client_id', sa.Text(), nullable=False),
        sa.Column('secret', sa.Text(), nullable=False),
        sa.Column('redirect_uri', sa.Text(), nullable=False),
        sa.Column('tenant_ids', sa.JSON(), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'enterprise_identity',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('connection_id', sa.Text(), nullable=False),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('profile', sa.JSON(), nullable=False),
        sa.UniqueConstraint('connection_id', 'tenant_id', 'subject', name='uq_enterprise_identity'),
    )
    op.create_index('ix_enterprise_identity_connection_id', 'enterprise_identity', ['connection_id'])
    op.create_index('ix_enterprise_identity_user_id', 'enterprise_identity', ['user_id'])
    op.create_table(
        'enterprise_login_state',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('connection_id', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('browser_nonce', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_enterprise_login_state_expires_at', 'enterprise_login_state', ['expires_at'])
    op.create_table(
        'quota_plan',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('unit', sa.Text(), nullable=False),
        sa.Column('allowance', sa.BigInteger(), nullable=False),
        sa.Column('reset', sa.JSON(), nullable=False),
        sa.Column('archived', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'quota_account',
        sa.Column('user_id', sa.Text(), primary_key=True),
        sa.Column('override_plan_id', sa.Text()),
        sa.Column('active_plan_id', sa.Text()),
        sa.Column('period_id', sa.Text()),
        sa.Column('pending_plan_id', sa.Text()),
        sa.Column('pending_inherit', sa.Boolean(), nullable=False),
        sa.Column('pending_at', sa.BigInteger()),
    )
    op.create_table(
        'quota_period',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('unit', sa.Text(), nullable=False),
        sa.Column('reset', sa.JSON(), nullable=False),
        sa.Column('starts_at', sa.BigInteger(), nullable=False),
        sa.Column('ends_at', sa.BigInteger()),
        sa.Column('base', sa.BigInteger(), nullable=False),
        sa.Column('bonus', sa.BigInteger(), nullable=False),
        sa.Column('used', sa.BigInteger(), nullable=False),
        sa.Column('reserved', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_quota_period_user_id', 'quota_period', ['user_id'])
    op.create_table(
        'model_price',
        sa.Column('model_id', sa.Text(), primary_key=True),
        sa.Column('expression', sa.Text(), nullable=False),
        sa.Column('multiplier', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'quota_entry',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('period_id', sa.Text(), nullable=False),
        sa.Column('model_id', sa.Text(), nullable=False),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('sent', sa.Boolean(), nullable=False),
        sa.Column('reserved', sa.BigInteger(), nullable=False),
        sa.Column('charged', sa.BigInteger()),
        sa.Column('pricing', sa.JSON()),
        sa.Column('usage', sa.JSON()),
        sa.Column('detail', sa.Text()),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('settled_at', sa.BigInteger()),
    )
    op.create_index('ix_quota_entry_period_id', 'quota_entry', ['period_id'])
    op.create_index('ix_quota_entry_user_created', 'quota_entry', ['user_id', 'created_at'])
    op.create_table(
        'quota_audit',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text()),
        sa.Column('actor_id', sa.Text(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_quota_audit_user_id', 'quota_audit', ['user_id'])
    op.create_table(
        'personal_connection',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('type', sa.Text(), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('secret', sa.Text(), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_personal_connection_user_id', 'personal_connection', ['user_id'])
    op.add_column('folder', sa.Column('kind', sa.Text(), nullable=False, server_default='folder'))
    op.add_column('chat', sa.Column('visibility', sa.Text(), nullable=False, server_default='private'))


def downgrade():
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT COUNT(*) FROM folder WHERE kind = 'project'")).scalar():
        raise RuntimeError('Export projects before downgrading: folder sharing has different semantics')
    if conn.execute(sa.text('SELECT COUNT(*) FROM quota_entry')).scalar():
        raise RuntimeError('Export the quota ledger before downgrading')
    op.drop_column('chat', 'visibility')
    op.drop_column('folder', 'kind')
    for table in (
        'personal_connection',
        'quota_audit',
        'quota_entry',
        'model_price',
        'quota_period',
        'quota_account',
        'quota_plan',
        'enterprise_login_state',
        'enterprise_identity',
        'enterprise_connection',
    ):
        op.drop_table(table)

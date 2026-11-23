"""Add per-request usage columns and the permanent hourly usage rollup; backfill retained requests."""

import json
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = 'c4a7e2b9d531'
down_revision = '30e4b65c927a'
branch_labels = None
depends_on = None

COLUMNS = ('prompt_tokens', 'completion_tokens', 'cache_read_tokens', 'cache_write_tokens', 'total_tokens', 'cost')


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def upgrade():
    with op.batch_alter_table('quota_entry') as batch:
        for column in COLUMNS:
            batch.add_column(sa.Column(column, sa.BigInteger()))
    op.create_index('ix_quota_entry_created', 'quota_entry', ['created_at'])
    op.create_table(
        'quota_usage_hourly',
        sa.Column('user_id', sa.Text(), primary_key=True),
        sa.Column('model_id', sa.Text(), primary_key=True),
        sa.Column('hour', sa.BigInteger(), primary_key=True),
        sa.Column('requests', sa.BigInteger(), nullable=False),
        *(sa.Column(column, sa.BigInteger(), nullable=False) for column in COLUMNS),
    )
    op.create_index('ix_quota_usage_hourly_hour', 'quota_usage_hourly', ['hour'])

    from open_webui.utils.quota_usage import usage_numbers

    conn = op.get_bind()
    entry = sa.table(
        'quota_entry',
        sa.column('id', sa.Text),
        *(sa.column(column, sa.BigInteger) for column in COLUMNS),
    )
    hourly = defaultdict(lambda: dict.fromkeys(('requests', *COLUMNS), 0))
    rows = conn.execute(
        sa.text(
            'SELECT e.id, e.user_id, e.model_id, e.created_at, e.charged, e.pricing, e.usage, p.unit '
            "FROM quota_entry e LEFT JOIN quota_period p ON p.id = e.period_id WHERE e.state = 'settled'"
        )
    ).all()
    for row in rows:
        values = usage_numbers(row.unit, row.charged, _json(row.pricing), _json(row.usage))
        conn.execute(sa.update(entry).where(entry.c.id == row.id).values(**values))
        bucket = hourly[(row.user_id, row.model_id, int(row.created_at) // 3600 * 3600)]
        bucket['requests'] += 1
        for column in COLUMNS:
            bucket[column] += values[column]
    if hourly:
        table = sa.table(
            'quota_usage_hourly',
            sa.column('user_id', sa.Text),
            sa.column('model_id', sa.Text),
            sa.column('hour', sa.BigInteger),
            sa.column('requests', sa.BigInteger),
            *(sa.column(column, sa.BigInteger) for column in COLUMNS),
        )
        op.bulk_insert(
            table,
            [{'user_id': u, 'model_id': m, 'hour': h, **values} for (u, m, h), values in hourly.items()],
        )


def downgrade():
    op.drop_index('ix_quota_usage_hourly_hour', table_name='quota_usage_hourly')
    op.drop_table('quota_usage_hourly')
    op.drop_index('ix_quota_entry_created', table_name='quota_entry')
    with op.batch_alter_table('quota_entry') as batch:
        for column in COLUMNS:
            batch.drop_column(column)

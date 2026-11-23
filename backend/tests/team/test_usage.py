import time
from zoneinfo import ZoneInfo

import pytest
from open_webui.internal.db import get_async_db
from open_webui.models.team import ModelPrice, QuotaEntry, QuotaUsageHourly
from open_webui.utils import quota_usage, quotas
from sqlalchemy import select
from test_quotas import setup


def test_tokens_are_normalized_across_providers():
    openai = quota_usage.usage_tokens(
        {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_tokens_details': {'cached_tokens': 60}}
    )
    assert openai == {
        'prompt_tokens': 100,
        'completion_tokens': 20,
        'cache_read_tokens': 60,
        'cache_write_tokens': 0,
        'total_tokens': 120,
    }
    # Anthropic reports cache reads and writes outside input_tokens.
    claude = quota_usage.usage_tokens(
        {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 30, 'cache_creation_input_tokens': 7},
        'anthropic',
    )
    assert claude['prompt_tokens'] == 47 and claude['total_tokens'] == 52 and claude['cache_write_tokens'] == 7
    assert quota_usage.usage_tokens(None)['total_tokens'] == 0
    assert quota_usage.usage_tokens({'prompt_tokens': 'bad'})['total_tokens'] == 0


async def price(model_id, expression):
    async with get_async_db() as db:
        db.add(ModelPrice(model_id=model_id, expression=expression, multiplier='1', revision=1, updated_at=0))
        await db.commit()


async def send(user_id, model_id, usage):
    entry = await quotas.reserve(user_id, model_id, {'prompt_tokens': 1, 'completion_tokens': 1})
    await quotas.mark_sent(entry)
    await quotas.settle(entry, usage)
    return entry


@pytest.mark.asyncio
async def test_settled_requests_feed_the_log_and_the_hourly_rollup():
    await setup(10**9, 'credits')
    await price('model', 'p * 2 + c * 8')  # USD per million tokens
    first = await send('alice', 'model', {'prompt_tokens': 1000, 'completion_tokens': 500})
    await send('alice', 'model', {'prompt_tokens': 1000, 'completion_tokens': 500})
    unsent = await quotas.reserve('alice', 'model', {})
    await quotas.settle(unsent)  # never sent: released, not counted
    async with get_async_db() as db:
        row = await db.get(QuotaEntry, first['id'])
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (1000, 500, 1500)
        assert row.cost == row.charged == 6000  # (1000*2 + 500*8) / 1e6 USD = 0.006 USD
        [hour] = (await db.execute(select(QuotaUsageHourly))).scalars().all()
    assert (hour.requests, hour.total_tokens, hour.cost) == (2, 3000, 12000)
    assert hour.hour == row.created_at // 3600 * 3600

    async with get_async_db() as db:
        rows = await quota_usage.hourly_rows(db, 0, int(time.time()) + 3600)
    stats = quota_usage.statistics(rows, tz_offset_minutes=480, by_user=True)
    assert stats['summary']['requests'] == 2 and stats['models'][0]['model_id'] == 'model'
    assert stats['users'][0]['user_id'] == 'alice' and len(stats['series']) == 1


@pytest.mark.asyncio
async def test_request_count_plans_still_show_what_requests_cost():
    await setup(100, 'requests')
    await price('priced', 'p * 2 + c * 8')
    priced = await send('bob', 'priced', {'prompt_tokens': 1000, 'completion_tokens': 500})
    free = await send('bob', 'unpriced', {'prompt_tokens': 10, 'completion_tokens': 5})
    async with get_async_db() as db:
        priced_row, free_row = await db.get(QuotaEntry, priced['id']), await db.get(QuotaEntry, free['id'])
    assert priced_row.charged == 1 and priced_row.cost == 6000
    assert free_row.charged == 1 and free_row.cost == 0 and free_row.total_tokens == 15
    summary = await quotas.quota_summary('bob')
    assert summary['period']['used'] == 2


def test_monthly_report_matches_the_reference_layout():
    zone = ZoneInfo('Asia/Shanghai')
    start = int(quota_usage.month_bounds('2026-10', zone)[0].timestamp())

    def row(user, model, hour, cost, tokens, requests=1):
        values = dict.fromkeys(quota_usage.SUM_FIELDS, 0)
        values.update(requests=requests, total_tokens=tokens, cost=cost)
        return QuotaUsageHourly(user_id=user, model_id=model, hour=hour, **values)

    rows = [
        row('a', 'gpt-big', start, 5_000_000, 1000, 3),
        row('a', 'gpt-small', start + 86400, 1_250_000, 4000),
        row('b', 'gpt-big', start + 2 * 86400, 2_000_000, 900, 2),
        row('gone', 'gpt-big', start, 10_000, 10),  # deleted account
    ]
    users = [('a', 'alice', 'acme'), ('b', 'bob', 'acme、beta'), ('c', 'carol', None)]
    report = quota_usage.render_monthly_report('Team', '2026-10', zone, rows, users, now=start)
    assert report.startswith('# Team 2026-10 使用记录月报')
    assert '- 统计周期：2026-10-01 00:00:00 至 2026-11-01 00:00:00（Asia/Shanghai）' in report
    assert '- 统计用户数：4' in report and '- 总花销：8.26 USD' in report and '- 请求数：7' in report
    assert '| 排名 | 用户 | 企业 ID | 总花销（USD） | 总 token 消耗 | 请求数 |' in report
    assert '| 1 | alice | acme | 6.25 | 5,000 | 4 |' in report
    assert '| 2 | bob | acme、beta | 2.00 | 900 | 2 |' in report
    assert '| 3 | gone | — | 0.01 | 10 | 1 |' in report  # deleted account
    assert '| 4 | carol | 本地账号 | 0.00 | 0 | 0 |' in report  # zero-usage users are listed last
    assert '- 企业 ID：acme' in report and '邮箱' not in report
    alice = report.split('### alice 的使用报告')[1].split('\n### ')[0]
    assert alice.index('| gpt-big | 5.00 |') < alice.index('| gpt-small | 1.25 |')
    assert '| 2026-10-01 | 5.00 | 1,000 |' in report
    carol = report.split('### carol 的使用报告')[1]
    assert '无模型统计数据。' in carol and '无按日统计数据。' in carol


@pytest.mark.asyncio
async def test_members_are_identified_by_enterprise_connect_id():
    from open_webui.models.team import EnterpriseIdentity
    from open_webui.routers import team

    async with get_async_db() as db:
        for index, (connection, user_id) in enumerate([('acme', 'u1'), ('beta', 'u1'), ('acme', 'u2')]):
            db.add(
                EnterpriseIdentity(
                    id=str(index),
                    connection_id=connection,
                    tenant_id='t',
                    subject=str(index),
                    user_id=user_id,
                    profile={},
                )
            )
        await db.commit()
        assert await team._orgs(db, ['u1', 'u2', 'local']) == {'u1': 'acme、beta', 'u2': 'acme'}
        assert await team._orgs(db, []) == {}

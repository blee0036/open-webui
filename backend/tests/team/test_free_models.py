from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from open_webui.internal.db import get_async_db
from open_webui.models.team import ModelPrice, QuotaEntry, QuotaPeriod, QuotaUsageHourly
from open_webui.utils import quotas
from open_webui.utils.quota_pricing import is_free, price_summary
from sqlalchemy import select
from test_quotas import setup

TIERED = (
    'len <= 200000 ? tier("standard", p * 3 + c * 15 + cr * 0.3) : tier("long_context", p * 6 + c * 22.5 + cr * 0.6)'
)


def test_price_summary_shows_base_and_long_context_rates():
    assert price_summary('p * 3 + c * 15') == {'input': 3.0, 'output': 15.0, 'long': None}
    assert price_summary(TIERED) == {
        'input': 3.0,
        'output': 15.0,
        'long': {'above': 200000, 'input': 6.0, 'output': 22.5},
    }
    assert price_summary('tier("image", fixed(0.04)) * image_count') == {'per_call': 0.04}
    assert price_summary('p * 2 + c * 8', '0.5')['output'] == 4.0  # the multiplier is applied


def test_only_prices_that_are_zero_for_all_usage_are_free():
    assert is_free('p * 0 + c * 0') and is_free('0') and is_free('tier("x", fixed(0))')
    assert is_free('p * 3 + c * 15', '0')
    assert not is_free('p * 0 + c * 1')
    assert not is_free('p * 0 + cr * 1')  # cache reads alone still cost
    assert not is_free('len > 5 ? p : 0')
    assert not is_free(TIERED)


async def prices(**expressions):
    async with get_async_db() as db:
        for model_id, expression in expressions.items():
            db.add(ModelPrice(model_id=model_id, expression=expression, multiplier='1', revision=1, updated_at=0))
        await db.commit()


async def drain(user_id, used):
    """Push the user's balance to `available - used` (negative balances allowed)."""
    summary = await quotas.quota_summary(user_id)
    async with get_async_db() as db:
        period = await db.get(QuotaPeriod, summary['period']['id'])
        period.used = used
        await db.commit()


async def send(user_id, model_id, usage):
    entry = await quotas.reserve(user_id, model_id, {'prompt_tokens': 10, 'completion_tokens': 10})
    await quotas.mark_sent(entry)
    await quotas.settle(entry, usage)
    return entry


@pytest.mark.asyncio
@pytest.mark.parametrize('unit', ['credits', 'requests'])
async def test_free_models_stay_usable_with_no_balance(unit):
    await setup(1000, unit)
    await prices(free='p * 0 + c * 0', paid='p * 3 + c * 15')
    await drain('member', 1500)  # balance is -500
    with pytest.raises(HTTPException) as error:
        await quotas.reserve('member', 'paid', {'prompt_tokens': 10, 'completion_tokens': 10})
    assert error.value.status_code == 429 and error.value.detail['code'] == 'quota_exhausted'

    entry = await send('member', 'free', {'prompt_tokens': 1000, 'completion_tokens': 500})
    summary = await quotas.quota_summary('member')
    assert summary['period']['used'] == 1500 and summary['period']['reserved'] == 0  # not charged
    async with get_async_db() as db:
        row = await db.get(QuotaEntry, entry['id'])
        assert row.state == 'settled' and row.charged == 0 and row.pricing['free']
        [hour] = (await db.execute(select(QuotaUsageHourly))).scalars().all()
    assert (hour.requests, hour.total_tokens, hour.cost) == (1, 1500, 0)


@pytest.mark.asyncio
async def test_member_price_list_has_rates_but_no_expressions():
    from open_webui.routers import team

    user = SimpleNamespace(id='viewer')
    assert (await team.model_prices(user))['enabled'] is False
    await setup(1000, 'credits')
    await prices(free='0', paid=TIERED, broken='p * ')
    result = await team.model_prices(user)
    assert result['enabled'] and result['unit'] == 'credits'
    assert result['prices']['free'] == {'free': True}
    assert result['prices']['paid']['long']['above'] == 200000 and not result['prices']['paid']['free']
    assert 'broken' not in result['prices'] and 'tier(' not in str(result)

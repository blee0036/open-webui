import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from open_webui.internal.db import get_async_db
from open_webui.models.team import EnterpriseIdentity, QuotaPeriod
from open_webui.routers import team
from open_webui.utils import quotas
from test_quotas import plan, setup
from test_security import member

ADMIN = SimpleNamespace(id='admin')


async def page(**params):
    defaults = {'query': '', 'plan_id': None, 'page': 1, 'page_size': 20}
    return await team.members(**(defaults | params), user=ADMIN)


@pytest.mark.asyncio
async def test_member_table_pages_filters_and_shows_balances():
    await setup(1000)
    await plan('pro', 5000)
    for name in ('alice', 'bob', 'carol'):
        await member(name)
    await member('waiting', role='pending')  # not a member yet
    async with get_async_db() as db:
        db.add(
            EnterpriseIdentity(id='1', connection_id='acme', tenant_id='t', subject='s', user_id='carol', profile={})
        )
        await db.commit()
    await quotas.assign_plan('bob', 'pro', 'admin')
    entry = await quotas.reserve('alice', 'model', {})
    await quotas.mark_sent(entry)
    await quotas.settle(entry, {'prompt_tokens': 1, 'completion_tokens': 1})

    result = await page()
    assert result['total'] == 3 and result['default_plan_id'] == 'default'
    rows = {item['id']: item for item in result['items']}
    assert rows['alice']['plan']['id'] == 'default' and rows['alice']['inherited']
    assert rows['alice']['available'] == 999 and rows['alice']['requests'] == 1
    assert rows['bob']['plan']['id'] == 'pro' and not rows['bob']['inherited']
    assert rows['carol']['org'] == 'acme'

    assert [i['id'] for i in (await page(query='acme'))['items']] == ['carol']  # enterprise ID search
    assert [i['id'] for i in (await page(plan_id='pro'))['items']] == ['bob']
    assert [i['id'] for i in (await page(plan_id='default'))['items']] == ['alice', 'carol']
    assert [i['id'] for i in (await page(plan_id='personal'))['items']] == ['bob']
    second = await page(page=2, page_size=2)
    assert second['total'] == 3 and [i['id'] for i in second['items']] == ['carol']


@pytest.mark.asyncio
async def test_package_list_counts_members():
    await setup(1000)
    await plan('pro', 5000)
    for name in ('alice', 'bob', 'carol'):
        await member(name)
    await quotas.assign_plan('bob', 'pro', 'admin')
    counts = {row['id']: (row['members'], row['default']) for row in await team.plans(user=ADMIN)}
    assert counts == {'default': (2, True), 'pro': (1, False)}


@pytest.mark.asyncio
async def test_adjustments_can_deduct_but_not_below_zero():
    await setup(1000)
    await member('alice')
    summary = await quotas.add_bonus('alice', -300, 'admin', 'Correction', str(uuid.uuid4()))
    assert summary['available'] == 700
    with pytest.raises(HTTPException):
        await quotas.add_bonus('alice', -800, 'admin', 'Too much', str(uuid.uuid4()))
    async with get_async_db() as db:
        period = await db.get(QuotaPeriod, summary['period']['id'])
        assert period.bonus == -300
    with pytest.raises(ValueError):
        team.BonusForm(amount=0, operation_id=uuid.uuid4())

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from open_webui.internal.db import get_async_db
from open_webui.models.config import Config
from open_webui.models.team import ModelPrice, QuotaPeriod, QuotaPlan
from open_webui.utils import quotas


async def plan(identifier, allowance=1000, unit='requests', kind='month'):
    async with get_async_db() as db:
        row = QuotaPlan(
            id=identifier,
            name=identifier,
            unit=unit,
            allowance=allowance,
            reset={'kind': kind, 'timezone': 'Asia/Singapore'},
            created_at=0,
            updated_at=0,
        )
        db.add(row)
        await db.commit()
    return identifier


async def setup(allowance=1000, unit='requests', kind='month'):
    await plan('default', allowance, unit, kind)
    await Config.upsert({'team.quota.enabled': True, 'team.quota.default_plan_id': 'default'})


@pytest.mark.asyncio
async def test_switch_bonus_reset_and_inheritance():
    await setup()
    summary = await quotas.quota_summary('member')
    async with get_async_db() as db:
        period = await db.get(QuotaPeriod, summary['period']['id'])
        period.used = 600
        await db.commit()
    await quotas.add_bonus('member', 200, 'admin', 'Extra project work', 'adjustment')
    await plan('larger', 2000)
    actual = await quotas.assign_plan('member', 'larger', 'admin')
    assert actual['available'] == 1600 and not actual['inherited']
    assert actual['period']['ends_at'] == summary['period']['ends_at']
    await Config.upsert({'team.quota.default_plan_id': 'larger'})
    await quotas.assign_plan('member', None, 'admin')
    actual = await quotas.quota_summary('member')
    assert actual['inherited'] and actual['available'] == 1600
    async with get_async_db() as db:
        row = await db.get(QuotaPeriod, actual['period']['id'])
        row.ends_at = int(time.time()) - 1
        await db.commit()
    actual = await quotas.quota_summary('member')
    assert actual['available'] == 2000 and actual['period']['bonus'] == 0


@pytest.mark.asyncio
async def test_atomic_concurrent_reservations_and_attempt_counts():
    await setup(2)
    outcomes = await asyncio.gather(*(quotas.reserve('user', 'model', {}) for _ in range(5)), return_exceptions=True)
    entries = [result for result in outcomes if isinstance(result, dict)]
    assert len(entries) == 2
    assert all(
        isinstance(result, HTTPException) and result.status_code == 429
        for result in outcomes
        if not isinstance(result, dict)
    )
    await quotas.settle(entries[0])  # pre-send failure: release
    await quotas.mark_sent(entries[1])
    await quotas.settle(entries[1], detail='HTTP 500')
    assert (await quotas.quota_summary('user'))['available'] == 1
    await quotas.settle(entries[1])  # idempotent
    assert (await quotas.quota_summary('user'))['period']['used'] == 1


@pytest.mark.asyncio
async def test_pending_overrun_frozen_price_and_original_period():
    await setup(100, 'credits')
    async with get_async_db() as db:
        db.add(ModelPrice(model_id='model', expression='p+c', multiplier='1', revision=1, updated_at=0))
        await db.commit()
    entry = await quotas.reserve('user', 'model', {'prompt_tokens': 20, 'completion_tokens': 10})
    await quotas.mark_sent(entry)
    await quotas.settle(entry)
    old = await quotas.quota_summary('user')
    assert old['period']['reserved'] == 30
    async with get_async_db() as db:
        (await db.get(ModelPrice, 'model')).expression = 'p*100+c*100'
        (await db.get(QuotaPeriod, old['period']['id'])).ends_at = int(time.time()) - 1
        await db.commit()
    new = await quotas.quota_summary('user')
    assert new['available'] == 100
    await quotas.settle(entry, {'prompt_tokens': 80, 'completion_tokens': 50})
    async with get_async_db() as db:
        row = await db.get(QuotaPeriod, old['period']['id'])
        assert row.used == 130 and row.reserved == 0
    assert (await quotas.quota_summary('user'))['available'] == 100


@pytest.mark.asyncio
async def test_incompatible_and_never_reset_switch():
    await setup(100, kind='never')
    await quotas.quota_summary('user')
    await plan('new', unit='credits')
    with pytest.raises(HTTPException):
        await quotas.assign_plan('user', 'new', 'admin')
    future = int(time.time()) + 100
    actual = await quotas.assign_plan('user', 'new', 'admin', effective_at=future)
    assert actual['pending_plan']['id'] == 'new' and actual['plan']['id'] == 'default'
    async with quotas.account_transaction('user') as (db, account):
        _, period = await quotas.current_period(db, account, 'default', future + 1)
        assert period.unit == 'credits'


@pytest.mark.asyncio
async def test_missing_price_and_explicit_zero():
    await setup(10, 'credits')
    with pytest.raises(HTTPException, match='') as error:
        await quotas.reserve('user', 'unknown', {})
    assert error.value.status_code == 400
    async with get_async_db() as db:
        db.add(ModelPrice(model_id='free', expression='0', multiplier='1', revision=1, updated_at=0))
        await db.commit()
    entry = await quotas.reserve('user', 'free', {})
    assert entry['reserved'] == 0


@pytest.mark.asyncio
async def test_default_change_replaces_or_cancels_inherited_pending_plan():
    await setup()
    original = await quotas.quota_summary('inheriting')
    await plan('daily', 200, kind='day')
    await Config.upsert({'team.quota.default_plan_id': 'daily'})
    pending = await quotas.quota_summary('inheriting')
    assert pending['pending_plan']['id'] == 'daily'
    await plan('compatible', 2000)
    await Config.upsert({'team.quota.default_plan_id': 'compatible'})
    current = await quotas.quota_summary('inheriting')
    assert current['pending_plan'] is None and current['pending_at'] is None
    assert current['plan']['id'] == 'compatible' and current['available'] == 2000
    assert current['period']['ends_at'] == original['period']['ends_at']


@pytest.mark.asyncio
async def test_default_never_change_is_atomic_and_handles_first_request_race():
    from open_webui.routers.team import SettingsForm, set_settings

    await setup(100, kind='never')
    await plan('monthly', 2000)
    admin = SimpleNamespace(id='admin')
    with pytest.raises(HTTPException) as error:
        await set_settings(SettingsForm(enabled=True, quota_enabled=True, default_plan_id='monthly'), admin)
    assert error.value.status_code == 409
    assert await Config.get('team.quota.default_plan_id') == 'default'

    original = await quotas.quota_summary('inherited')
    await quotas.assign_plan('personal', 'default', 'admin')
    future = int(time.time()) + 3600
    await set_settings(
        SettingsForm(enabled=True, quota_enabled=True, default_plan_id='monthly', default_effective_at=future), admin
    )
    pending = await quotas.quota_summary('inherited')
    assert pending['pending_at'] == future and pending['pending_plan']['id'] == 'monthly'
    personal = await quotas.quota_summary('personal')
    assert personal['plan']['id'] == 'default' and personal['pending_plan'] is None
    assert not personal['inherited']
    async with quotas.account_transaction('inherited') as (db, account):
        # A first reservation that raced the admin's account scan still reads
        # the committed default transition rather than rejecting/resetting it.
        account.pending_at = account.pending_plan_id = None
        account.pending_inherit = False
        _, period = await quotas.current_period(db, account, 'monthly')
        assert period.id == original['period']['id'] and account.pending_at == future
        _, period = await quotas.current_period(db, account, 'monthly', future + 1)
        assert period.base == 2000 and period.id != original['period']['id']


@pytest.mark.asyncio
async def test_explicit_pending_package_survives_default_changes():
    from open_webui.routers.team import SettingsForm, set_settings

    await setup(100, kind='never')
    await quotas.quota_summary('member')
    await plan('personal-daily', 500, kind='day')
    await plan('new-default', 2000, kind='month')
    future = int(time.time()) + 3600
    await quotas.assign_plan('member', 'personal-daily', 'admin', effective_at=future)
    await set_settings(
        SettingsForm(enabled=True, quota_enabled=True, default_plan_id='new-default', default_effective_at=future + 60),
        SimpleNamespace(id='admin'),
    )
    actual = await quotas.quota_summary('member')
    assert actual['pending_plan']['id'] == 'personal-daily' and actual['pending_at'] == future
    async with quotas.account_transaction('member') as (db, account):
        plan_value, period = await quotas.current_period(db, account, 'new-default', future + 1)
        assert plan_value.id == 'personal-daily' and period.base == 500
        assert account.override_plan_id == 'personal-daily'

    # The same precedence applies to inherited calendar periods.
    await quotas.assign_plan('calendar', 'new-default', 'admin')
    await quotas.assign_plan('calendar', None, 'admin')
    await quotas.assign_plan('calendar', 'personal-daily', 'admin')
    await Config.upsert({'team.quota.default_plan_id': 'personal-daily'})
    await plan('another-default', 900, unit='credits')
    await Config.upsert({'team.quota.default_plan_id': 'another-default'})
    assert (await quotas.quota_summary('calendar'))['pending_plan']['id'] == 'personal-daily'


@pytest.mark.asyncio
async def test_bonus_retry_is_idempotent_only_for_identical_operation():
    await setup()
    first = await quotas.add_bonus('user', 200, 'admin', 'Project', 'op')
    retry = await quotas.add_bonus('user', 200, 'admin', 'Project', 'op')
    assert first['available'] == retry['available'] == 1200
    for user, amount, actor, reason in [
        ('other', 200, 'admin', 'Project'),
        ('user', 201, 'admin', 'Project'),
        ('user', 200, 'other', 'Project'),
        ('user', 200, 'admin', 'Different'),
    ]:
        with pytest.raises(HTTPException) as error:
            await quotas.add_bonus(user, amount, actor, reason, 'op')
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_scheduled_fixed_interval_uses_effective_time_not_first_visit():
    await setup(100, kind='never')
    await quotas.quota_summary('member')
    async with get_async_db() as db:
        db.add(
            QuotaPlan(
                id='weekly',
                name='Every 7 days',
                unit='requests',
                allowance=700,
                reset={'kind': 'days', 'timezone': 'Asia/Singapore', 'days': 7},
                created_at=0,
                updated_at=0,
            )
        )
        await db.commit()
    future = int(time.time()) + 100
    await quotas.assign_plan('member', 'weekly', 'admin', effective_at=future)
    async with quotas.account_transaction('member') as (db, account):
        _, period = await quotas.current_period(db, account, 'default', future + 10 * 86400)
        assert period.starts_at == future + 7 * 86400 and period.ends_at == future + 14 * 86400


@pytest.mark.asyncio
async def test_per_call_price_settles_without_usage_and_per_unit():
    await setup(10**9, 'credits')
    async with get_async_db() as db:
        db.add(
            ModelPrice(
                model_id='sd-checkpoint',
                expression='tier("image", fixed(0.01)) * image_count',
                multiplier='1',
                revision=1,
                updated_at=0,
            )
        )
        db.add(ModelPrice(model_id='chat', expression='p+c', multiplier='1', revision=1, updated_at=0))
        await db.commit()
    entry = await quotas.reserve('user', 'sd-checkpoint', {}, image_count=3)
    assert entry['reserved'] == 30000
    await quotas.mark_sent(entry)
    await quotas.settle(entry)  # A1111/ComfyUI report no usage
    async with get_async_db() as db:
        row = await db.get(quotas.QuotaEntry, entry['id'])
        assert row.state == 'settled' and row.charged == 30000

    # An upstream error keeps the reservation for reconciliation instead of charging.
    failed = await quotas.reserve('user', 'sd-checkpoint', {}, image_count=2)
    await quotas.mark_sent(failed)
    await quotas.settle(failed, detail='HTTP 500')
    async with get_async_db() as db:
        assert (await db.get(quotas.QuotaEntry, failed['id'])).state == 'pending'

    # Token prices without usage still wait for reconciliation.
    token = await quotas.reserve('user', 'chat', {'prompt_tokens': 1, 'completion_tokens': 1})
    await quotas.mark_sent(token)
    await quotas.settle(token)
    async with get_async_db() as db:
        assert (await db.get(quotas.QuotaEntry, token['id'])).state == 'pending'


@pytest.mark.asyncio
async def test_mark_sent_persists_without_account_lock_and_ignores_settled_entries():
    await setup(10)
    entry = await quotas.reserve('user', 'model', {})
    await quotas.mark_sent(entry)
    async with get_async_db() as db:
        assert (await db.get(quotas.QuotaEntry, entry['id'])).sent is True
    await quotas.settle(entry)
    await quotas.mark_sent(entry)  # late trace callback after settlement is a no-op
    async with get_async_db() as db:
        assert (await db.get(quotas.QuotaEntry, entry['id'])).state == 'settled'


@pytest.mark.asyncio
async def test_old_request_history_is_purged_and_held_reservations_returned():
    from open_webui.models.team import QuotaEntry
    from sqlalchemy import select, update

    await setup(10)
    old = [await quotas.reserve('member', 'model', {}) for _ in range(4)]
    await quotas.settle(old[0])  # never sent: released
    await quotas.mark_sent(old[1])
    await quotas.settle(old[1])  # settled, charged 1
    # old[2] stays reserved; old[3] waits for reconciliation and keeps its hold
    recent = await quotas.reserve('member', 'model', {})
    now = int(time.time())
    async with get_async_db() as db:
        await db.execute(
            update(QuotaEntry)
            .where(QuotaEntry.id.in_([entry['id'] for entry in old]))
            .values(created_at=now - 91 * 86400)
        )
        await db.execute(update(QuotaEntry).where(QuotaEntry.id == old[3]['id']).values(state='pending', sent=True))
        await db.commit()
    before = await quotas.quota_summary('member')
    assert before['period']['used'] == 1 and before['period']['reserved'] == 3

    assert await quotas.purge_old_entries(now=now, days=0) == 0
    assert await quotas.purge_old_entries(now=now, days=90) == 4
    async with get_async_db() as db:
        left = (await db.execute(select(QuotaEntry.id))).scalars().all()
    assert left == [recent['id']]
    after = await quotas.quota_summary('member')
    # Settled usage stays; the two abandoned holds go back to the member.
    assert after['period']['used'] == 1 and after['period']['reserved'] == 1
    assert await quotas.purge_old_entries(now=now, days=90) == 0

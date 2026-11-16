"""Upstream price sync against a local New-API-shaped server; no external network."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from fastapi import HTTPException
from open_webui.internal.db import get_async_db
from open_webui.models.team import ModelPrice, QuotaAudit
from open_webui.routers.team import (
    PriceForm,
    SyncApplyForm,
    SyncCompareForm,
    set_price,
    sync_apply,
    sync_compare,
)
from sqlalchemy import select

PRICING = [
    # Claude ratio with cache read and the default 1.25 write (always dual-TTL for Claude names).
    {'model_name': 'claude-sonnet-4-5', 'quota_type': 0, 'model_ratio': 1.5, 'completion_ratio': 5, 'cache_ratio': 0.1},
    {'model_name': 'gpt-image-1', 'quota_type': 1, 'model_price': 0.04},
    {'model_name': 'unchanged', 'quota_type': 0, 'model_ratio': 1},
    {'model_name': 'unknown-default', 'quota_type': 0, 'model_ratio': 37.5, 'completion_ratio': 1},
]


@pytest_asyncio.fixture
async def upstream():
    async def pricing(request):
        return web.json_response({'success': True, 'data': PRICING})

    async def broken(request):
        return web.Response(status=500)

    app = web.Application()
    app.router.add_get('/api/pricing', pricing)
    app.router.add_get('/broken', broken)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        yield f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    finally:
        await runner.cleanup()


def request(models=()):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(MODELS={name: {} for name in models})))


@pytest.mark.asyncio
async def test_compare_then_apply_upstream_prices(upstream):
    admin = SimpleNamespace(id='admin')
    await set_price('unchanged', PriceForm(expression='tier("base", p * 2 + c * 2)', reason='Existing'), admin)
    form = SyncCompareForm(
        upstreams=[
            {'name': 'gateway', 'base_url': upstream},
            {'name': 'down', 'base_url': upstream, 'endpoint': '/broken'},
        ],
        timeout=5,
    )
    result = await sync_compare(form, request(['claude-sonnet-4-5']), admin)

    assert {source['name']: source['status'] for source in result['sources']} == {'gateway': 'success', 'down': 'error'}
    rows = {row['model_id']: row for row in result['models']}
    assert 'unchanged' not in rows  # identical price is not a difference
    claude = rows['claude-sonnet-4-5']['upstreams']['gateway']
    assert claude['expression'] == 'tier("base", p * 3 + c * 15 + cr * 0.3 + cc * 3.75 + cc1h * 6)'
    assert rows['claude-sonnet-4-5']['available'] is True
    assert rows['gpt-image-1']['upstreams']['gateway']['expression'] == 'tier("image", fixed(0.04)) * image_count'
    assert rows['unknown-default']['upstreams']['gateway']['trusted'] is False

    applied = await sync_apply(
        SyncApplyForm(
            reason='Sync from gateway',
            items=[
                {'model_id': 'claude-sonnet-4-5', 'expression': claude['expression'], 'source': 'gateway'},
                {'model_id': 'unchanged', 'expression': 'tier("base", p * 2 + c * 4)', 'source': 'gateway'},
            ],
        ),
        admin,
    )
    assert applied == {'applied': 2}
    async with get_async_db() as db:
        kept = await db.get(ModelPrice, 'unchanged')
        assert kept.revision == 2 and kept.multiplier == '1'
        audits = (await db.execute(select(QuotaAudit).where(QuotaAudit.action == 'price'))).scalars().all()
    synced = [row for row in audits if row.data.get('source') == 'sync:gateway']
    assert len(synced) == 2 and all(row.data['reason'] == 'Sync from gateway' for row in synced)


@pytest.mark.asyncio
async def test_apply_is_atomic_and_rejects_invalid_prices():
    admin = SimpleNamespace(id='admin')
    with pytest.raises(HTTPException):
        await sync_apply(
            SyncApplyForm(
                reason='Bad sync',
                items=[
                    {'model_id': 'ok', 'expression': 'tier("base", p)', 'source': 'x'},
                    {'model_id': 'bad', 'expression': 'tier("x", fixed(1) + p)', 'source': 'x'},
                ],
            ),
            admin,
        )
    async with get_async_db() as db:
        assert (await db.execute(select(ModelPrice))).scalars().all() == []

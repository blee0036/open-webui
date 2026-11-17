from types import SimpleNamespace

import pytest
from open_webui.internal.db import get_async_db
from open_webui.models.team import QuotaAudit
from open_webui.routers.team import PlanForm, PriceForm, create_plan, set_price, update_plan
from pydantic import ValidationError
from sqlalchemy import select


def test_configuration_adjustment_notes_are_optional_and_bounded():
    for form, values in [
        (PlanForm, {'name': 'Standard', 'unit': 'requests', 'allowance': 1000}),
        (PriceForm, {'expression': 'p*3+c*15'}),
    ]:
        assert form(**values).reason == ''
        assert form(**values, reason='  \t\n').reason == ''
        with pytest.raises(ValidationError):
            form(**values, reason='x' * 501)


@pytest.mark.asyncio
async def test_package_edits_record_operator_reason_and_before_after():
    admin = SimpleNamespace(id='admin')
    created = await create_plan(
        PlanForm(name='Standard', unit='requests', allowance=1000, reason='  Initial allocation  '), admin
    )
    updated = await update_plan(
        created['id'],
        PlanForm(name='Standard', unit='requests', allowance=2000, reason='Project expansion'),
        admin,
    )
    assert updated['allowance'] == 2000
    async with get_async_db() as db:
        rows = (await db.execute(select(QuotaAudit))).scalars().all()
    audits = {row.action: row for row in rows}
    assert len(audits) == 2
    assert all(row.actor_id == admin.id for row in rows)
    assert audits['create_plan'].data['reason'] == 'Initial allocation'
    assert audits['create_plan'].data['after']['allowance'] == 1000
    change = audits['update_plan'].data
    assert change['plan_id'] == created['id'] and change['reason'] == 'Project expansion'
    assert change['before']['allowance'] == 1000 and change['after']['allowance'] == 2000


@pytest.mark.asyncio
async def test_price_revisions_keep_operator_reason_and_previous_price():
    admin = SimpleNamespace(id='admin')
    await set_price('model', PriceForm(expression='p*3+c*15', reason='Provider list price'), admin)
    updated = await set_price('model', PriceForm(expression='0', reason='Local model is free'), admin)
    assert updated['revision'] == 2 and updated['expression'] == '0'
    async with get_async_db() as db:
        rows = (await db.execute(select(QuotaAudit).where(QuotaAudit.action == 'price'))).scalars().all()
    assert len(rows) == 2 and all(row.actor_id == admin.id for row in rows)
    audits = {row.data['reason']: row.data for row in rows}
    assert audits['Provider list price']['before'] is None
    change = audits['Local model is free']
    assert change['model_id'] == 'model'
    assert change['before']['expression'] == 'p*3+c*15' and change['before']['revision'] == 1
    assert change['after']['expression'] == '0' and change['after']['revision'] == 2

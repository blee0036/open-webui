"""Package administration and the signed-in user's quota view."""

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from open_webui.internal.db import get_async_db
from open_webui.utils import quota_sync
from open_webui.models.config import Config
from open_webui.models.team import ModelPrice, QuotaAccount, QuotaAudit, QuotaEntry, QuotaPlan
from open_webui.models.users import User
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.quota_periods import validate_reset
from open_webui.utils.quota_pricing import MAX_EXPRESSION_LENGTH, MAX_QUOTA, validate_price
from open_webui.utils.quotas import add_bonus, assign_plan, quota_summary, record, settle
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, or_, select, update

router = APIRouter()


class AdjustmentForm(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator('reason', mode='before')
    @classmethod
    def reason_valid(cls, value):
        return value.strip() if isinstance(value, str) else value


class PlanForm(AdjustmentForm):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    unit: str
    allowance: int = Field(ge=0, le=MAX_QUOTA)
    reset: dict = Field(default_factory=lambda: {'kind': 'month', 'timezone': 'Asia/Singapore'})
    archived: bool = False

    @field_validator('unit')
    @classmethod
    def unit_valid(cls, value):
        if value not in {'requests', 'credits'}:
            raise ValueError('Unit must be requests or credits')
        return value

    @field_validator('reset')
    @classmethod
    def reset_valid(cls, value):
        return validate_reset(value)


class SettingsForm(AdjustmentForm):
    enabled: bool
    quota_enabled: bool
    default_plan_id: str | None = None
    default_effective_at: int | None = None
    reason: str = Field(default='Default package configuration', min_length=1, max_length=500)


@router.get('/admin/settings')
async def settings(user=Depends(get_admin_user)):
    return {
        'enabled': await Config.get('team.enabled', False),
        'quota_enabled': await Config.get('team.quota.enabled', False),
        'default_plan_id': await Config.get('team.quota.default_plan_id'),
        'sandbox_configured': bool(await Config.get('team.sandbox.url')),
    }


@router.put('/admin/settings')
async def set_settings(form: SettingsForm, user=Depends(get_admin_user)):
    if form.quota_enabled and not form.default_plan_id:
        raise HTTPException(400, 'Configure a default package before enabling quota enforcement')
    async with get_async_db() as db:
        async with db.begin():
            # Serialize against individual assignments before reading inheritance.
            # Updating the config and the inherited schedules is one transaction.
            await db.execute(
                update(QuotaAccount).where(QuotaAccount.override_plan_id.is_(None)).values(user_id=QuotaAccount.user_id)
            )
            old_default = await db.get(Config, 'team.quota.default_plan_id')
            old_id = old_default.value if old_default else Config.default_value('team.quota.default_plan_id')
            old_plan = await db.get(QuotaPlan, old_id) if old_id else None
            plan = await db.get(QuotaPlan, form.default_plan_id) if form.default_plan_id else None
            if form.default_plan_id and (not plan or plan.archived):
                raise HTTPException(400, 'Choose an active default package')
            incompatible_never = (
                old_plan
                and plan
                and old_plan.reset['kind'] == 'never'
                and (old_plan.reset != plan.reset or old_plan.unit != plan.unit)
            )
            if incompatible_never and (form.default_effective_at is None or form.default_effective_at <= time.time()):
                raise HTTPException(409, 'Specify a future effective time for non-resetting inherited accounts')
            if plan:
                accounts = (
                    await db.execute(select(QuotaAccount).where(QuotaAccount.override_plan_id.is_(None)))
                ).scalars()
                for account in accounts:
                    if account.pending_at is not None and not account.pending_inherit:
                        continue  # Do not replace an explicitly scheduled personal package.
                    if account.active_plan_id:
                        active = await db.get(QuotaPlan, account.active_plan_id)
                        if (
                            active
                            and active.reset['kind'] == 'never'
                            and (active.reset != plan.reset or active.unit != plan.unit)
                        ):
                            if form.default_effective_at is None or form.default_effective_at <= time.time():
                                raise HTTPException(
                                    409, 'Specify a future effective time for non-resetting inherited accounts'
                                )
                            account.pending_plan_id = plan.id
                            account.pending_inherit = True
                            account.pending_at = form.default_effective_at
            db.add(
                QuotaAudit(
                    id=str(uuid.uuid4()),
                    actor_id=user.id,
                    action='default_plan',
                    data={
                        'plan_id': form.default_plan_id,
                        'reason': form.reason,
                        'enabled': form.enabled,
                        'quota_enabled': form.quota_enabled,
                        'effective_at': form.default_effective_at,
                    },
                    created_at=int(time.time()),
                )
            )
            await Config.upsert(
                {
                    'team.enabled': form.enabled,
                    'team.quota.enabled': form.quota_enabled,
                    'team.quota.default_plan_id': form.default_plan_id,
                    'team.quota.default_transition': {
                        'plan_id': form.default_plan_id,
                        'effective_at': form.default_effective_at,
                    },
                },
                db=db,
            )
    from open_webui.utils.team_policy import initialize_team_permissions

    await initialize_team_permissions()
    return await settings(user)


@router.get('/quota/me')
async def my_quota(user=Depends(get_verified_user)):
    return await quota_summary(user.id)


@router.get('/quota/me/entries')
async def my_entries(
    before: int | None = None,
    before_id: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=100),
    user=Depends(get_verified_user),
):
    async with get_async_db() as db:
        stmt = select(QuotaEntry).where(QuotaEntry.user_id == user.id)
        if before is not None:
            stmt = (
                stmt.where(
                    or_(
                        QuotaEntry.created_at < before, and_(QuotaEntry.created_at == before, QuotaEntry.id > before_id)
                    )
                )
                if before_id
                else stmt.where(QuotaEntry.created_at < before)
            )
        return [
            record(row)
            for row in (
                await db.execute(stmt.order_by(QuotaEntry.created_at.desc(), QuotaEntry.id).limit(limit))
            ).scalars()
        ]


@router.get('/admin/plans')
async def plans(user=Depends(get_admin_user)):
    async with get_async_db() as db:
        return [record(row) for row in (await db.execute(select(QuotaPlan).order_by(QuotaPlan.created_at))).scalars()]


@router.post('/admin/plans')
async def create_plan(form: PlanForm, user=Depends(get_admin_user)):
    now = int(time.time())
    async with get_async_db() as db:
        row = QuotaPlan(id=str(uuid.uuid4()), **form.model_dump(exclude={'reason'}), created_at=now, updated_at=now)
        db.add(row)
        db.add(
            QuotaAudit(
                id=str(uuid.uuid4()),
                actor_id=user.id,
                action='create_plan',
                data={'plan_id': row.id, 'after': record(row), 'reason': form.reason},
                created_at=now,
            )
        )
        await db.commit()
        return record(row)


@router.put('/admin/plans/{plan_id}')
async def update_plan(plan_id: str, form: PlanForm, user=Depends(get_admin_user)):
    async with get_async_db() as db:
        row = await db.get(QuotaPlan, plan_id)
        if not row:
            raise HTTPException(404, 'Package not found')
        if row.unit != form.unit or row.reset != form.reset:
            raise HTTPException(
                400, 'Create a new package to change its unit or reset rule; existing periods are preserved'
            )
        if form.archived and await Config.get('team.quota.default_plan_id') == plan_id:
            raise HTTPException(400, 'Choose another default before archiving this package')
        before = record(row)
        for key, value in form.model_dump(exclude={'reason'}).items():
            setattr(row, key, value)
        row.updated_at = int(time.time())
        db.add(
            QuotaAudit(
                id=str(uuid.uuid4()),
                actor_id=user.id,
                action='update_plan',
                data={'plan_id': row.id, 'before': before, 'after': record(row), 'reason': form.reason},
                created_at=row.updated_at,
            )
        )
        await db.commit()
        return record(row)


class AssignmentForm(AdjustmentForm):
    plan_id: str | None = None
    effective_at: int | None = None
    reason: str = Field(default='Package assignment', min_length=1, max_length=500)


class BonusForm(AdjustmentForm):
    amount: int = Field(gt=0, le=MAX_QUOTA)
    operation_id: uuid.UUID


@router.get('/admin/users')
async def users(query: str = '', limit: int = Query(50, ge=1, le=100), user=Depends(get_admin_user)):
    async with get_async_db() as db:
        stmt = select(User.id, User.name, User.email, User.role).order_by(User.name).limit(limit)
        if query:
            escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            stmt = stmt.where(
                User.name.ilike(f'%{escaped}%', escape='\\') | User.email.ilike(f'%{escaped}%', escape='\\')
            )
        return [dict(row._mapping) for row in (await db.execute(stmt)).all()]


@router.get('/admin/users/{user_id}/quota')
async def user_quota(user_id: str, user=Depends(get_admin_user)):
    return await quota_summary(user_id)


@router.put('/admin/users/{user_id}/plan')
async def set_plan(user_id: str, form: AssignmentForm, user=Depends(get_admin_user)):
    async with get_async_db() as db:
        if not await db.get(User, user_id):
            raise HTTPException(404, 'User not found')
    return await assign_plan(user_id, form.plan_id, user.id, effective_at=form.effective_at, reason=form.reason)


@router.post('/admin/users/{user_id}/bonus')
async def bonus(user_id: str, form: BonusForm, user=Depends(get_admin_user)):
    async with get_async_db() as db:
        if not await db.get(User, user_id):
            raise HTTPException(404, 'User not found')
    return await add_bonus(user_id, form.amount, user.id, form.reason, str(form.operation_id))


class PriceForm(AdjustmentForm):
    expression: str = Field(min_length=1, max_length=MAX_EXPRESSION_LENGTH)
    multiplier: str = '1'

    @field_validator('expression')
    @classmethod
    def expression_valid(cls, value):
        validate_price(value.strip())
        return value.strip()


@router.get('/admin/prices')
async def prices(user=Depends(get_admin_user)):
    async with get_async_db() as db:
        return [record(row) for row in (await db.execute(select(ModelPrice).order_by(ModelPrice.model_id))).scalars()]


def write_price(db, row, model_id, expression, multiplier, actor_id, reason, source=None):
    before = record(row) if row else None
    if not row:
        row = ModelPrice(model_id=model_id, revision=0)
        db.add(row)
    row.expression, row.multiplier = expression, multiplier
    row.revision += 1
    row.updated_at = int(time.time())
    db.add(
        QuotaAudit(
            id=str(uuid.uuid4()),
            actor_id=actor_id,
            action='price',
            data={
                'model_id': model_id,
                'before': before,
                'after': record(row),
                'reason': reason,
                **({'source': source} if source else {}),
            },
            created_at=row.updated_at,
        )
    )
    return row


@router.put('/admin/prices/{model_id:path}')
async def set_price(model_id: str, form: PriceForm, user=Depends(get_admin_user)):
    try:
        validate_price(form.expression, form.multiplier)
    except (ValueError, SyntaxError) as error:
        raise HTTPException(400, str(error)) from error
    async with get_async_db() as db:
        row = write_price(
            db, await db.get(ModelPrice, model_id), model_id, form.expression, form.multiplier, user.id, form.reason
        )
        await db.commit()
        return record(row)


class UpstreamForm(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(default='', max_length=2048)
    endpoint: str = Field(default='', max_length=2048)
    # OpenRouter only; used for this request and never stored.
    api_key: str | None = Field(default=None, max_length=1024)


class SyncCompareForm(BaseModel):
    upstreams: list[UpstreamForm] = Field(min_length=1, max_length=20)
    timeout: int = Field(default=10, ge=1, le=60)
    include_unchanged: bool = False


@router.get('/admin/prices/sync/sources')
async def sync_sources(user=Depends(get_admin_user)):
    return {'presets': quota_sync.PRESETS, 'endpoints': quota_sync.ENDPOINT_OPTIONS}


@router.post('/admin/prices/sync/compare')
async def sync_compare(form: SyncCompareForm, request: Request, user=Depends(get_admin_user)):
    names = [upstream.name for upstream in form.upstreams]
    if len(set(names)) != len(names):
        raise HTTPException(400, 'Upstream names must be unique')
    async with get_async_db() as db:
        local = {row.model_id: row.expression for row in (await db.execute(select(ModelPrice))).scalars()}
    result = await quota_sync.compare(
        [upstream.model_dump() for upstream in form.upstreams],
        local,
        timeout=form.timeout,
        include_unchanged=form.include_unchanged,
    )
    available = set((getattr(request.app.state, 'MODELS', None) or {}).keys())
    for row in result['models']:
        row['available'] = row['model_id'] in available
    return result


class SyncItem(BaseModel):
    model_id: str = Field(min_length=1, max_length=512)
    expression: str = Field(min_length=1, max_length=MAX_EXPRESSION_LENGTH)
    source: str = Field(min_length=1, max_length=100)


class SyncApplyForm(AdjustmentForm):
    items: list[SyncItem] = Field(min_length=1, max_length=5000)


@router.post('/admin/prices/sync/apply')
async def sync_apply(form: SyncApplyForm, user=Depends(get_admin_user)):
    """Apply selected upstream prices atomically; each change is a normal audited price version."""
    if len({item.model_id for item in form.items}) != len(form.items):
        raise HTTPException(400, 'Each model can be applied once per sync')
    for item in form.items:
        try:
            validate_price(item.expression.strip())
        except ValueError as error:
            raise HTTPException(400, f'{item.model_id}: {error}') from error
    async with get_async_db() as db:
        async with db.begin():
            for item in form.items:
                row = await db.get(ModelPrice, item.model_id)
                write_price(
                    db,
                    row,
                    item.model_id,
                    item.expression.strip(),
                    row.multiplier if row else '1',
                    user.id,
                    form.reason,
                    source=f'sync:{item.source}',
                )
    return {'applied': len(form.items)}


@router.get('/admin/entries')
async def entries(state: str | None = None, user_id: str | None = None, user=Depends(get_admin_user)):
    async with get_async_db() as db:
        stmt = select(QuotaEntry).order_by(QuotaEntry.created_at.desc()).limit(100)
        if state:
            stmt = stmt.where(QuotaEntry.state == state)
        if user_id:
            stmt = stmt.where(QuotaEntry.user_id == user_id)
        return [record(row) for row in (await db.execute(stmt)).scalars()]


@router.get('/admin/audit')
async def audit(
    user_id: str | None = None,
    before: int | None = None,
    before_id: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=100),
    user=Depends(get_admin_user),
):
    async with get_async_db() as db:
        stmt = select(QuotaAudit)
        if user_id:
            stmt = stmt.where(QuotaAudit.user_id == user_id)
        if before is not None:
            stmt = (
                stmt.where(
                    or_(
                        QuotaAudit.created_at < before, and_(QuotaAudit.created_at == before, QuotaAudit.id > before_id)
                    )
                )
                if before_id
                else stmt.where(QuotaAudit.created_at < before)
            )
        return [
            record(row)
            for row in (
                await db.execute(stmt.order_by(QuotaAudit.created_at.desc(), QuotaAudit.id).limit(limit))
            ).scalars()
        ]


class ReconcileForm(AdjustmentForm):
    charge: int = Field(ge=0, le=MAX_QUOTA)


@router.post('/admin/entries/{entry_id}/reconcile')
async def reconcile(entry_id: str, form: ReconcileForm, user=Depends(get_admin_user)):
    async with get_async_db() as db:
        entry = await db.get(QuotaEntry, entry_id)
        if not entry or entry.state != 'pending':
            raise HTTPException(409, 'Only pending entries can be reconciled')
        value = record(entry)
    await settle(value, detail=form.reason, override_charge=form.charge, actor_id=user.id)
    return {'status': True}

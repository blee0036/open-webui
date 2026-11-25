"""Package administration and the signed-in user's quota view."""

import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from open_webui.env import WEBUI_NAME
from open_webui.internal.db import get_async_db
from open_webui.utils import quota_sync, quota_usage
from open_webui.models.config import Config
from open_webui.models.team import (
    EnterpriseIdentity,
    ModelPrice,
    QuotaAccount,
    QuotaAudit,
    QuotaEntry,
    QuotaPlan,
)
from open_webui.models.users import User
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.quota_periods import validate_reset
from open_webui.utils.quota_pricing import (
    MAX_EXPRESSION_LENGTH,
    MAX_QUOTA,
    is_free,
    price_summary,
    validate_price,
)
from open_webui.utils.quotas import add_bonus, assign_plan, quota_summary, record, settle
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func, or_, select, update

router = APIRouter()


class AdjustmentForm(BaseModel):
    # Optional note for the adjustment history, which already records who changed what.
    reason: str = Field(default='', max_length=500)

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


@router.get('/prices')
async def model_prices(user=Depends(get_verified_user)):
    """Rates for the model list (USD per million tokens or per call); expressions stay admin-only."""
    summary = await quota_summary(user.id)
    if not summary.get('enabled'):
        return {'enabled': False, 'unit': None, 'prices': {}}
    prices = {}
    async with get_async_db() as db:
        for row in (await db.execute(select(ModelPrice))).scalars():
            try:
                free = is_free(row.expression, row.multiplier)
                prices[row.model_id] = {'free': free, **({} if free else price_summary(row.expression, row.multiplier))}
            except ValueError:
                continue  # An unusable price is reported to admins on the price page, not here.
    return {'enabled': True, 'unit': summary['plan']['unit'], 'prices': prices}


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
    """Packages with how many members use each: personal assignments, plus everyone else on the default."""
    default_id = await Config.get('team.quota.default_plan_id')
    async with get_async_db() as db:
        rows = (await db.execute(select(QuotaPlan).order_by(QuotaPlan.created_at))).scalars().all()
        personal = dict(
            (
                await db.execute(
                    select(QuotaAccount.override_plan_id, func.count())
                    .join(User, User.id == QuotaAccount.user_id)
                    .where(QuotaAccount.override_plan_id.is_not(None), User.role != 'pending')
                    .group_by(QuotaAccount.override_plan_id)
                )
            ).all()
        )
        member_count = await db.scalar(select(func.count()).select_from(User).where(User.role != 'pending'))
    inherited = max(0, (member_count or 0) - sum(personal.values()))
    return [
        {
            **record(row),
            'default': row.id == default_id,
            'members': personal.get(row.id, 0) + (inherited if row.id == default_id else 0),
            'personal_members': personal.get(row.id, 0),
        }
        for row in rows
    ]


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


class BonusForm(AdjustmentForm):
    # Positive adds to this period; negative deducts (both expire at the next reset).
    amount: int = Field(ge=-MAX_QUOTA, le=MAX_QUOTA)
    operation_id: uuid.UUID

    @field_validator('amount')
    @classmethod
    def amount_nonzero(cls, value):
        if value == 0:
            raise ValueError('Enter an amount to add or deduct')
        return value


@router.get('/admin/users')
async def users(query: str = '', limit: int = Query(50, ge=1, le=100), user=Depends(get_admin_user)):
    async with get_async_db() as db:
        stmt = select(User.id, User.name, User.email, User.role).order_by(User.name).limit(limit)
        if query:
            escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            stmt = stmt.where(
                User.name.ilike(f'%{escaped}%', escape='\\') | User.email.ilike(f'%{escaped}%', escape='\\')
            )
        rows = [dict(row._mapping) for row in (await db.execute(stmt)).all()]
        orgs = await _orgs(db, [row['id'] for row in rows])
    return [{**row, 'org': orgs.get(row['id'])} for row in rows]


@router.get('/admin/members')
async def members(
    query: str = Query('', max_length=200),
    plan_id: str | None = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_admin_user),
):
    """One page of members with their package and this period's balance (the member-quota table)."""
    default_id = await Config.get('team.quota.default_plan_id')
    async with get_async_db() as db:
        stmt = (
            select(User.id, User.name, User.email, User.role)
            .outerjoin(QuotaAccount, QuotaAccount.user_id == User.id)
            .where(User.role != 'pending')
        )
        if query:
            escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            pattern = f'%{escaped}%'
            enterprise = select(EnterpriseIdentity.user_id).where(
                EnterpriseIdentity.connection_id.ilike(pattern, escape='\\')
            )
            stmt = stmt.where(
                User.name.ilike(pattern, escape='\\') | User.email.ilike(pattern, escape='\\') | User.id.in_(enterprise)
            )
        if plan_id == 'personal':
            stmt = stmt.where(QuotaAccount.override_plan_id.is_not(None))
        elif plan_id == 'inherited':
            stmt = stmt.where(QuotaAccount.override_plan_id.is_(None))
        elif plan_id:
            chosen = QuotaAccount.override_plan_id == plan_id
            stmt = stmt.where(chosen | QuotaAccount.override_plan_id.is_(None) if plan_id == default_id else chosen)
        total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            await db.execute(stmt.order_by(User.name, User.id).offset((page - 1) * page_size).limit(page_size))
        ).all()
        orgs = await _orgs(db, [row.id for row in rows])
    items = []
    for row in rows:
        item = {'id': row.id, 'name': row.name, 'email': row.email, 'role': row.role, 'org': orgs.get(row.id)}
        try:
            item |= await quota_summary(row.id)
        except HTTPException as error:
            item['error'] = error.detail
        items.append(item)
    periods = [item['period']['id'] for item in items if item.get('period')]
    if periods:
        async with get_async_db() as db:
            counts = dict(
                (
                    await db.execute(
                        select(QuotaEntry.period_id, func.count())
                        .where(QuotaEntry.period_id.in_(periods), QuotaEntry.state == 'settled')
                        .group_by(QuotaEntry.period_id)
                    )
                ).all()
            )
        for item in items:
            if item.get('period'):
                item['requests'] = counts.get(item['period']['id'], 0)
    return {'items': items, 'total': total, 'default_plan_id': default_id}


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


# ---------------------------------------------------------------------------
# Usage statistics, request log and monthly report
# ---------------------------------------------------------------------------

MAX_RANGE = 366 * 86400
LOG_FIELDS = (
    'id',
    'user_id',
    'model_id',
    'state',
    'detail',
    'created_at',
    'settled_at',
    'charged',
    'reserved',
    'prompt_tokens',
    'completion_tokens',
    'cache_read_tokens',
    'cache_write_tokens',
    'total_tokens',
    'cost',
)


def _range(start: int | None, end: int | None):
    end = int(end if end is not None else time.time()) + 1
    start = int(start if start is not None else end - 7 * 86400)
    if start >= end or end - start > MAX_RANGE:
        raise HTTPException(400, 'Choose a range of at most one year')
    return start, end


async def _orgs(db, ids=None):
    """Enterprise (SSO connect) IDs per user, joined: SSO members often have no email."""
    stmt = select(EnterpriseIdentity.user_id, EnterpriseIdentity.connection_id).distinct()
    if ids is not None:
        if not ids:
            return {}
        stmt = stmt.where(EnterpriseIdentity.user_id.in_(list(ids)))
    found = {}
    for user_id, connection_id in (await db.execute(stmt)).all():
        found.setdefault(user_id, []).append(connection_id)
    return {user_id: '、'.join(sorted(values)) for user_id, values in found.items()}


async def _names(db, ids):
    if not ids:
        return {}
    stmt = select(User.id, User.name).where(User.id.in_(list(ids)))
    orgs = await _orgs(db, ids)
    return {row.id: (row.name, orgs.get(row.id)) for row in (await db.execute(stmt)).all()}


async def _statistics(start, end, tz_offset, *, user_id=None, model_id=None, by_user=False):
    async with get_async_db() as db:
        rows = await quota_usage.hourly_rows(db, start, end, user_id=user_id, model_id=model_id)
        result = quota_usage.statistics(rows, tz_offset_minutes=tz_offset, by_user=by_user)
        names = await _names(db, {item['user_id'] for item in result['users']})
    for item in result['users']:
        item['name'], item['org'] = names.get(item['user_id'], (None, None))
    return result


async def _logs(start, end, page, page_size, *, user_id=None, model_id=None, state=None, with_users=False):
    conditions = [QuotaEntry.created_at >= start, QuotaEntry.created_at < end]
    if user_id:
        conditions.append(QuotaEntry.user_id == user_id)
    if model_id:
        conditions.append(QuotaEntry.model_id == model_id)
    if state:
        conditions.append(QuotaEntry.state == state)
    async with get_async_db() as db:
        total = (await db.execute(select(func.count()).select_from(QuotaEntry).where(*conditions))).scalar_one()
        rows = (
            await db.execute(
                select(QuotaEntry)
                .where(*conditions)
                .order_by(QuotaEntry.created_at.desc(), QuotaEntry.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
        items = []
        for row in rows:
            item = {field: getattr(row, field) for field in LOG_FIELDS}
            pricing = row.pricing or {}
            item['unit'] = pricing.get('unit') or ('credits' if row.pricing else 'requests')
            item['tier'] = (pricing.get('settlement') or {}).get('tier')
            items.append(item)
        names = await _names(db, {item['user_id'] for item in items}) if with_users else {}
    for item in items if with_users else ():
        item['name'], item['org'] = names.get(item['user_id'], (None, None))
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size}


@router.get('/usage/me')
async def my_usage(
    start: int | None = None,
    end: int | None = None,
    tz_offset: int = Query(0, ge=-840, le=840),
    model_id: str | None = Query(None, max_length=500),
    user=Depends(get_verified_user),
):
    start, end = _range(start, end)
    return await _statistics(start, end, tz_offset, user_id=user.id, model_id=model_id)


@router.get('/usage/me/logs')
async def my_usage_logs(
    start: int | None = None,
    end: int | None = None,
    model_id: str | None = Query(None, max_length=500),
    state: str | None = Query(None, max_length=20),
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_verified_user),
):
    start, end = _range(start, end)
    return await _logs(start, end, page, page_size, user_id=user.id, model_id=model_id, state=state)


@router.get('/admin/usage')
async def admin_usage(
    start: int | None = None,
    end: int | None = None,
    tz_offset: int = Query(0, ge=-840, le=840),
    user_id: str | None = Query(None, max_length=100),
    model_id: str | None = Query(None, max_length=500),
    user=Depends(get_admin_user),
):
    start, end = _range(start, end)
    return await _statistics(start, end, tz_offset, user_id=user_id, model_id=model_id, by_user=True)


@router.get('/admin/usage/logs')
async def admin_usage_logs(
    start: int | None = None,
    end: int | None = None,
    user_id: str | None = Query(None, max_length=100),
    model_id: str | None = Query(None, max_length=500),
    state: str | None = Query(None, max_length=20),
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_admin_user),
):
    start, end = _range(start, end)
    return await _logs(start, end, page, page_size, user_id=user_id, model_id=model_id, state=state, with_users=True)


@router.get('/admin/usage/report')
async def admin_usage_report(
    month: str = Query(pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    tz: str = Query('Asia/Shanghai', max_length=64),
    user=Depends(get_admin_user),
):
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, 'Unknown time zone') from None
    start, end = quota_usage.month_bounds(month, zone)
    async with get_async_db() as db:
        rows = await quota_usage.hourly_rows(db, int(start.timestamp()), int(end.timestamp()))
        people = (await db.execute(select(User.id, User.name).where(User.role != 'pending'))).all()
        orgs = await _orgs(db)
    users = [(row.id, row.name, orgs.get(row.id)) for row in people]
    body = quota_usage.render_monthly_report(WEBUI_NAME, month, zone, rows, users)
    return Response(
        body,
        media_type='text/markdown; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="usage_report_{month}.md"'},
    )

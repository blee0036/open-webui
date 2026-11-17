"""Transactional quota accounting; no database transaction spans an upstream request."""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import HTTPException
from open_webui.env import QUOTA_ENTRY_RETENTION_DAYS
from open_webui.internal.db import get_async_db
from open_webui.models.config import Config
from open_webui.models.team import ModelPrice, QuotaAccount, QuotaAudit, QuotaEntry, QuotaPeriod, QuotaPlan
from open_webui.utils.quota_periods import period_bounds
from open_webui.utils.quota_pricing import (
    MAX_QUOTA,
    freeze_request,
    is_per_call,
    normalize_image_count,
    price_usage,
    request_snapshot,
)
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

log = logging.getLogger(__name__)


def record(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns} if row else None


async def account_config(db, key, default=None):
    """Read quota policy after taking the account lock, never from a stale snapshot."""
    if not Config.persistent_enabled_for(key):
        return Config.default_value(key, default)
    row = await db.get(Config, key)
    return row.value if row else Config.default_value(key, default)


@asynccontextmanager
async def account_transaction(user_id):
    async with get_async_db() as db:
        async with db.begin():
            dialect = db.bind.dialect.name
            if dialect in {'sqlite', 'postgresql'}:
                insert = sqlite_insert if dialect == 'sqlite' else pg_insert
                await db.execute(
                    insert(QuotaAccount)
                    .values(user_id=user_id, pending_inherit=False)
                    .on_conflict_do_nothing(index_elements=['user_id'])
                )
            elif not await db.get(QuotaAccount, user_id):
                db.add(QuotaAccount(user_id=user_id, pending_inherit=False))
                await db.flush()
            # UPDATE is a real write lock on both SQLite and PostgreSQL.
            await db.execute(update(QuotaAccount).where(QuotaAccount.user_id == user_id).values(user_id=user_id))
            account = await db.get(QuotaAccount, user_id, populate_existing=True)
            yield db, account


async def current_period(db, account, default_plan_id, now=None):
    now = int(time.time()) if now is None else now
    period = await db.get(QuotaPeriod, account.period_id) if account.period_id else None
    effective_at = None
    if account.pending_at is not None and account.pending_at <= now:
        effective_at = account.pending_at
        account.override_plan_id = None if account.pending_inherit else account.pending_plan_id
        account.pending_plan_id = None
        account.pending_inherit = False
        account.pending_at = None
        # A scheduled change creates a new period, leaving old reservations attached to the old ledger.
        period = None
    desired_id = account.override_plan_id or default_plan_id
    desired = await db.get(QuotaPlan, desired_id) if desired_id else None
    if not desired:
        raise HTTPException(503, detail={'code': 'quota_plan_missing', 'message': 'No quota package is configured.'})
    if period and period.ends_at is not None and now >= period.ends_at:
        period = None
    if period and (period.unit != desired.unit or period.reset != desired.reset):
        # A new inherited default or edited plan also follows the scheduled-change rule.
        if period.ends_at is None:
            if account.pending_at is None:
                transition = await account_config(db, 'team.quota.default_transition', {})
                when = transition.get('effective_at') if transition.get('plan_id') == desired.id else None
                if account.override_plan_id is None and when is not None:
                    # Covers an account's first reservation racing the default
                    # change's scan, without resetting or blocking that account.
                    account.pending_plan_id, account.pending_inherit, account.pending_at = desired.id, True, when
                    if when <= now:
                        return await current_period(db, account, default_plan_id, now)
                else:
                    raise HTTPException(
                        409,
                        detail={
                            'code': 'quota_effective_time_required',
                            'message': 'An effective time is required to change a non-resetting package.',
                        },
                    )
        elif account.pending_at is None or account.pending_inherit:
            # A scheduled personal assignment takes precedence over changes to
            # the inherited default, even before that assignment becomes active.
            account.pending_plan_id = desired.id
            account.pending_inherit = account.override_plan_id is None
            account.pending_at = period.ends_at
        active = await db.get(QuotaPlan, account.active_plan_id)
        return active, period
    if not period:
        anchor = effective_at
        if anchor is None and account.period_id:
            previous = await db.get(QuotaPeriod, account.period_id)
            if previous and previous.reset == desired.reset and previous.ends_at is not None:
                anchor = previous.ends_at
        starts, ends = period_bounds(desired.reset, now, anchor)
        period = QuotaPeriod(
            id=str(uuid.uuid4()),
            user_id=account.user_id,
            unit=desired.unit,
            reset=desired.reset,
            starts_at=starts,
            ends_at=ends,
            base=desired.allowance,
            bonus=0,
            used=0,
            reserved=0,
        )
        db.add(period)
        account.period_id = period.id
    else:
        period.base = desired.allowance
        if account.override_plan_id is None and account.pending_inherit:
            # The inherited default returned to a compatible package before the
            # scheduled boundary. Do not display a stale pending default.
            account.pending_plan_id = None
            account.pending_at = None
            account.pending_inherit = False
    account.active_plan_id = desired.id
    await db.flush()
    return desired, period


async def quota_summary(user_id):
    async with account_transaction(user_id) as (db, account):
        enabled = await account_config(db, 'team.quota.enabled', False)
        default_id = await account_config(db, 'team.quota.default_plan_id')
        if not enabled and not default_id and not account.override_plan_id:
            return {'enabled': False, 'configured': False}
        plan, period = await current_period(db, account, default_id)
        pending = await db.get(QuotaPlan, account.pending_plan_id) if account.pending_plan_id else None
        available = period.base + period.bonus - period.used - period.reserved
        return {
            'enabled': enabled,
            'configured': True,
            'plan': record(plan),
            'inherited': account.override_plan_id is None,
            'period': record(period),
            'available': max(0, available),
            'overage': max(0, -available),
            'pending_plan': record(pending),
            'pending_at': account.pending_at,
        }


async def assign_plan(user_id, plan_id, actor_id, *, effective_at=None, reason='Package assignment'):
    async with account_transaction(user_id) as (db, account):
        default_id = await account_config(db, 'team.quota.default_plan_id')
        target = await db.get(QuotaPlan, plan_id or default_id) if (plan_id or default_id) else None
        if not target or target.archived:
            raise HTTPException(400, 'Choose an active quota package')
        period = await db.get(QuotaPeriod, account.period_id) if account.period_id else None
        now = int(time.time())
        same = period is None or period.unit == target.unit and period.reset == target.reset
        if same or period.ends_at is not None and period.ends_at <= now:
            account.override_plan_id = plan_id
            account.pending_at = None
            account.pending_plan_id = None
            account.pending_inherit = False
            await current_period(db, account, default_id, now)
            when = now
        else:
            when = period.ends_at or effective_at
            if when is None or when <= now:
                raise HTTPException(400, 'Choose a future effective time for a non-resetting package')
            account.pending_plan_id = target.id
            account.pending_inherit = plan_id is None
            account.pending_at = when
        db.add(
            QuotaAudit(
                id=str(uuid.uuid4()),
                user_id=user_id,
                actor_id=actor_id,
                action='assign',
                data={'plan_id': plan_id, 'effective_at': when, 'reason': reason},
                created_at=now,
            )
        )
    return await quota_summary(user_id)


async def add_bonus(user_id, amount, actor_id, reason, operation_id):
    async with account_transaction(user_id) as (db, account):
        default_id = await account_config(db, 'team.quota.default_plan_id')
        existing = await db.get(QuotaAudit, operation_id)
        if existing:
            if (
                existing.user_id != user_id
                or existing.actor_id != actor_id
                or existing.action != 'bonus'
                or existing.data.get('amount') != amount
                or existing.data.get('reason') != reason
            ):
                raise HTTPException(409, 'Adjustment ID was already used for a different operation')
        else:
            _, period = await current_period(db, account, default_id)
            if period.base + period.bonus + amount > MAX_QUOTA:
                raise HTTPException(400, 'Quota adjustment exceeds the supported limit')
            period.bonus += amount
            db.add(
                QuotaAudit(
                    id=operation_id,
                    user_id=user_id,
                    actor_id=actor_id,
                    action='bonus',
                    data={'amount': amount, 'reason': reason, 'period_id': period.id},
                    created_at=int(time.time()),
                )
            )
    return await quota_summary(user_id)


async def reserve(
    user_id,
    model_id,
    estimated_usage,
    *,
    provider='openai',
    price_model_id=None,
    image_count=1,
    payload=None,
    headers=None,
):
    image_count = normalize_image_count(image_count)
    if not await Config.get('team.quota.enabled', False):
        return None
    async with account_transaction(user_id) as (db, account):
        if not await account_config(db, 'team.quota.enabled', False):
            return None
        default_id = await account_config(db, 'team.quota.default_plan_id')
        _, period = await current_period(db, account, default_id)
        snapshot = None
        amount = 1
        if period.unit == 'credits':
            price = await db.get(ModelPrice, model_id)
            if not price and price_model_id:
                price = await db.get(ModelPrice, price_model_id)
            if not price:
                raise HTTPException(
                    400, detail={'code': 'model_price_missing', 'message': 'Model price is not configured.'}
                )
            snapshot = {
                'model_id': price.model_id,
                'expression': price.expression,
                'multiplier': price.multiplier,
                'revision': price.revision,
                'provider': provider,
                'credits_per_usd': 1_000_000,
                'image_count': image_count,
            }
            try:
                # Frozen with the price: only the request fields the expression reads.
                request = request_snapshot(price.expression, payload, headers)
                if request:
                    snapshot['request'] = freeze_request(request)
                amount = price_usage(
                    price.expression,
                    estimated_usage,
                    provider=provider,
                    multiplier=price.multiplier,
                    image_count=image_count,
                    request=request,
                )['credits']
            except ValueError as error:
                raise HTTPException(400, detail={'code': 'model_price_error', 'message': str(error)}) from error
        available = period.base + period.bonus - period.used - period.reserved
        if available < amount or available <= 0:
            raise HTTPException(
                429,
                detail={
                    'code': 'quota_exhausted',
                    'message': 'Your package quota is exhausted.',
                    'reset_at': period.ends_at,
                    'available': max(0, available),
                },
            )
        entry = QuotaEntry(
            id=str(uuid.uuid4()),
            user_id=user_id,
            period_id=period.id,
            model_id=model_id,
            state='reserved',
            sent=False,
            reserved=amount,
            pricing=snapshot,
            created_at=int(time.time()),
        )
        db.add(entry)
        period.reserved += amount
        return record(entry)


def per_call_price(pricing) -> bool:
    """Every branch is tier(name, fixed(x)): New-API charges it even with zero reported tokens."""
    return bool(pricing and pricing.get('expression')) and is_per_call(pricing['expression'])


def _settle_price(pricing, usage):
    return price_usage(
        pricing['expression'],
        usage,
        provider=pricing['provider'],
        multiplier=pricing['multiplier'],
        credits_per_usd=pricing['credits_per_usd'],
        image_count=pricing.get('image_count', pricing.get('units', 1)),
        request=pricing.get('request'),
    )


async def mark_sent(entry):
    """Durably flag the attempt as sent for crash-time reconciliation.

    settle() already honours the in-memory flag, so this touches only the entry
    row and never takes the per-account lock that reserve/settle serialize on.
    """
    if not entry:
        return
    entry['sent'] = True
    async with get_async_db() as db:
        await db.execute(
            update(QuotaEntry)
            .where(QuotaEntry.id == entry['id'], QuotaEntry.state == 'reserved', QuotaEntry.sent.is_(False))
            .values(sent=True)
        )
        await db.commit()


async def settle(entry, usage=None, detail=None, *, override_charge=None, actor_id=None):
    if not entry:
        return
    async with account_transaction(entry['user_id']) as (db, _):
        row = await db.get(QuotaEntry, entry['id'])
        if not row or row.state in {'settled', 'released'}:
            return
        row.sent = bool(row.sent or entry.get('sent'))
        period = await db.get(QuotaPeriod, row.period_id)
        if not row.sent:
            charge, state = 0, 'released'
        elif override_charge is not None:
            charge, state = override_charge, 'settled'
        elif period.unit == 'requests':
            charge, state = 1, 'settled'
        elif usage is None and detail is None and per_call_price(row.pricing):
            # Per-call prices (new-api "按次", e.g. fixed(0.04)) need no upstream usage.
            # Local image backends (A1111/ComfyUI) and some image APIs never report tokens.
            # An upstream error (detail set) stays pending for reconciliation instead.
            result = _settle_price(row.pricing, {})
            charge, state = result['credits'], 'settled'
            row.pricing = {**row.pricing, 'settlement': result}
        elif usage is None:
            row.state, row.detail = 'pending', detail or 'Upstream usage is unavailable; reservation retained.'
            return
        else:
            try:
                result = _settle_price(row.pricing, usage)
                charge, state = result['credits'], 'settled'
                row.pricing = {**row.pricing, 'settlement': result}
            except ValueError as error:
                row.state, row.detail, row.usage = 'pending', str(error), usage
                return
        period.reserved -= row.reserved
        period.used += charge
        row.charged, row.state, row.usage, row.detail = charge, state, usage, detail
        row.settled_at = int(time.time())
        if actor_id:
            db.add(
                QuotaAudit(
                    id=str(uuid.uuid4()),
                    user_id=row.user_id,
                    actor_id=actor_id,
                    action='reconcile',
                    data={'entry_id': row.id, 'charge': charge, 'reason': detail},
                    created_at=row.settled_at,
                )
            )


async def purge_old_entries(now=None, days=None):
    """Delete request-history rows older than the retention window, whatever their state.

    Balances live on quota_period, so settled/released rows can go without touching them.
    A row still holding a reservation (reserved, or pending reconciliation) gives that
    amount back to its period first, so an abandoned request cannot pin quota forever.
    """
    days = QUOTA_ENTRY_RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0
    cutoff = int(now if now is not None else time.time()) - days * 86400
    async with get_async_db() as db:
        async with db.begin():
            held = (
                await db.execute(
                    select(QuotaEntry.period_id, func.sum(QuotaEntry.reserved))
                    .where(QuotaEntry.created_at < cutoff, QuotaEntry.state.in_(('reserved', 'pending')))
                    .group_by(QuotaEntry.period_id)
                )
            ).all()
            for period_id, amount in held:
                await db.execute(
                    update(QuotaPeriod)
                    .where(QuotaPeriod.id == period_id)
                    .values(
                        reserved=case(
                            (QuotaPeriod.reserved > amount, QuotaPeriod.reserved - amount),
                            else_=0,
                        )
                    )
                )
            result = await db.execute(delete(QuotaEntry).where(QuotaEntry.created_at < cutoff))
    return result.rowcount or 0


async def periodic_quota_entry_purge(interval=6 * 3600):
    while True:
        try:
            removed = await purge_old_entries()
            if removed:
                log.info('Removed %d request-history rows older than %d days', removed, QUOTA_ENTRY_RETENTION_DAYS)
        except Exception:
            log.exception('Request-history cleanup failed')
        await asyncio.sleep(interval)

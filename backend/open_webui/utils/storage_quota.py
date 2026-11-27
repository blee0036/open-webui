"""Storage caps from quota packages: uploaded files (hard) and sandbox home volumes (soft).

Usage is never stored separately. Files are summed from their recorded sizes, so deleting a
file frees its space at once; sandbox volumes are measured by the controller and cached there.
"""

import asyncio
import logging
import time

from fastapi import HTTPException
from open_webui.internal.db import get_async_db
from open_webui.models.config import Config
from open_webui.models.files import File
from open_webui.models.team import QuotaAccount, QuotaPlan
from open_webui.models.users import User
from sqlalchemy import case, func, select

log = logging.getLogger(__name__)

SYNC_INTERVAL = 300


async def limits_enabled() -> bool:
    """Storage caps follow the same switch as quota enforcement."""
    return bool(await Config.get('team.enabled', False) and await Config.get('team.quota.enabled', False))


def _plan_id(account, default_id, now):
    """The package that applies now, without creating periods or taking the account lock."""
    if account is None:
        return default_id
    if account.pending_at is not None and account.pending_at <= now:
        return default_id if account.pending_inherit else account.pending_plan_id
    return account.override_plan_id or default_id


async def plan_limits(user_ids=None, db=None) -> dict[str, dict]:
    """{user_id: {'storage_bytes', 'sandbox_bytes', 'plan_id', 'plan_name'}}; all members when user_ids is None."""
    default_id = await Config.get('team.quota.default_plan_id')
    now = int(time.time())

    async def run(session):
        plans = {plan.id: plan for plan in (await session.execute(select(QuotaPlan))).scalars()}
        stmt = select(User.id, User.role, QuotaAccount).outerjoin(QuotaAccount, QuotaAccount.user_id == User.id)
        stmt = stmt.where(User.role != 'pending') if user_ids is None else stmt.where(User.id.in_(user_ids))
        result = {}
        for user_id, role, account in (await session.execute(stmt)).all():
            plan = plans.get(_plan_id(account, default_id, now))
            result[user_id] = {
                'storage_bytes': plan.storage_bytes if plan else None,
                'sandbox_bytes': plan.sandbox_bytes if plan else None,
                'plan_id': plan.id if plan else None,
                'plan_name': plan.name if plan else None,
                'role': role,
            }
        return result

    if db is not None:
        return await run(db)
    async with get_async_db() as session:
        return await run(session)


async def user_limits(user) -> dict:
    """Caps that apply to this member now; None means unlimited (admins and quota-off included)."""
    if getattr(user, 'role', None) == 'admin' or not await limits_enabled():
        return {'storage_bytes': None, 'sandbox_bytes': None}
    limits = (await plan_limits([user.id])).get(user.id) or {}
    return {'storage_bytes': limits.get('storage_bytes'), 'sandbox_bytes': limits.get('sandbox_bytes')}


_size = File.meta['size'].as_integer()


async def file_usage(user_id, db=None) -> dict:
    async def run(session):
        row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(_size), 0),
                    func.count(File.id),
                    func.sum(case((_size.is_(None), 1), else_=0)),
                ).where(File.user_id == user_id)
            )
        ).one()
        return {'used': int(row[0] or 0), 'file_count': int(row[1] or 0), 'unknown_size': int(row[2] or 0)}

    if db is not None:
        return await run(db)
    async with get_async_db() as session:
        return await run(session)


async def file_usage_by_user(db) -> dict[str, dict]:
    rows = (
        await db.execute(
            select(
                File.user_id,
                func.coalesce(func.sum(_size), 0),
                func.count(File.id),
                func.sum(case((_size.is_(None), 1), else_=0)),
            ).group_by(File.user_id)
        )
    ).all()
    return {
        user_id: {'used': int(used or 0), 'file_count': int(count or 0), 'unknown_size': int(unknown or 0)}
        for user_id, used, count, unknown in rows
    }


def storage_exceeded(used, limit, incoming):
    return {
        'code': 'storage_quota_exceeded',
        'used': used,
        'limit': limit,
        'incoming': incoming,
        'message': 'Storage quota exceeded. Delete files you no longer need or ask an administrator for more space.',
    }


async def check_file_quota(user, incoming: int | None, db=None):
    """Raise 413 if storing `incoming` more bytes would pass the member's file cap."""
    limit = (await user_limits(user))['storage_bytes']
    if limit is None:
        return
    used = (await file_usage(user.id, db))['used']
    if used + max(0, incoming or 0) > limit:
        raise HTTPException(413, detail=storage_exceeded(used, limit, incoming or 0))


# --- Sandbox caps live in the controller; WebUI pushes them from the packages. ---

_DISK_CACHE_SECONDS = 30
_disk_cache: dict[str, tuple[float, dict | None]] = {}


async def sandbox_disk(user_id) -> dict | None:
    """The controller's last measurement for this member, or None when there is no sandbox.

    The quota badge polls this, so answers are cached briefly and the controller is asked
    through a read-only endpoint that never creates a workspace.
    """
    from urllib.parse import quote

    from open_webui.utils.sandboxes import admin_controller_request, sandbox_configured

    if not await Config.get('team.enabled', False) or not await sandbox_configured():
        return None
    cached = _disk_cache.get(user_id)
    if cached and time.monotonic() - cached[0] < _DISK_CACHE_SECONDS:
        return cached[1]
    try:
        value = await admin_controller_request(f'/admin/users/{quote(user_id, safe="")}/disk', timeout=3)
        value = {key: value.get(key) for key in ('used', 'over', 'scanned_at')}
    except Exception:
        value = None
    _disk_cache[user_id] = (time.monotonic(), value)
    return value


def forget_sandbox_disk(user_id=None):
    if user_id is None:
        _disk_cache.clear()
    else:
        _disk_cache.pop(user_id, None)


async def sync_sandbox_limits():
    """Push every member's sandbox cap to the controller. Failures are logged, never raised."""
    from open_webui.utils.sandboxes import admin_controller_request, sandbox_configured

    try:
        if not await Config.get('team.enabled', False) or not await sandbox_configured():
            return
        if await limits_enabled():
            limits = {
                user_id: value['sandbox_bytes']
                for user_id, value in (await plan_limits()).items()
                if value['role'] != 'admin'  # Administrators are exempt, as for uploads.
            }
        else:
            limits = {}  # Enforcement off: lift every cap.
        await admin_controller_request('/admin/limits', 'PUT', {'limits': limits})
    except Exception:
        log.exception('Could not sync sandbox storage limits')


def schedule_sandbox_limit_sync():
    """Fire-and-forget push after a package or assignment change."""
    try:
        asyncio.get_running_loop().create_task(sync_sandbox_limits())
    except RuntimeError:
        pass


async def periodic_sandbox_limit_sync(interval=SYNC_INTERVAL):
    # Catches scheduled package switches and any change made while the controller was down.
    while True:
        await sync_sandbox_limits()
        await asyncio.sleep(interval)

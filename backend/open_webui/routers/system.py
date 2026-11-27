"""Administrator system view: host resources, storage per member and managed sandboxes."""

import asyncio
import logging
import os
import time

import psutil
from fastapi import APIRouter, Depends, HTTPException
from open_webui.config import STORAGE_PROVIDER, UPLOAD_DIR
from open_webui.env import DATA_DIR
from open_webui.internal.db import get_async_db
from open_webui.models.config import Config
from open_webui.models.folders import Folder
from open_webui.models.notifications import emit_changes, insert_notifications
from open_webui.models.team import QuotaAudit
from open_webui.models.users import User
from open_webui.utils.auth import get_admin_user
from open_webui.utils.sandboxes import admin_controller_request, sandbox_configured
from open_webui.utils.storage_quota import file_usage_by_user, forget_sandbox_disk, limits_enabled, plan_limits
from pydantic import BaseModel
from sqlalchemy import select, text

log = logging.getLogger(__name__)
router = APIRouter()

_PROCESS = psutil.Process(os.getpid())
# Prime the CPU counters so the first overview reports a real percentage.
psutil.cpu_percent(interval=None)
_PROCESS.cpu_percent(interval=None)


def _disk(path):
    try:
        usage = psutil.disk_usage(str(path))
        return {'path': str(path), 'total': usage.total, 'used': usage.used, 'free': usage.free}
    except OSError:
        return None


def _host():
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    try:
        load = list(os.getloadavg())
    except (AttributeError, OSError):  # Windows
        load = None
    with _PROCESS.oneshot():
        process = {
            'rss': _PROCESS.memory_info().rss,
            'cpu_percent': _PROCESS.cpu_percent(interval=None),
            'started_at': int(_PROCESS.create_time()),
            'threads': _PROCESS.num_threads(),
        }
    disks = [_disk(DATA_DIR)]
    upload_disk = _disk(UPLOAD_DIR)
    if upload_disk and disks[0] and os.stat(DATA_DIR).st_dev != os.stat(UPLOAD_DIR).st_dev:
        disks.append(upload_disk)
    return {
        'hostname': os.uname().nodename if hasattr(os, 'uname') else os.environ.get('COMPUTERNAME'),
        'cpus': psutil.cpu_count(),
        'cpu_percent': psutil.cpu_percent(interval=None),
        'load': load,
        'memory': {'total': memory.total, 'available': memory.available, 'used': memory.total - memory.available},
        'swap': {'total': swap.total, 'used': swap.used},
        'disks': [disk for disk in disks if disk],
        'process': process,
        'boot_at': int(psutil.boot_time()),
    }


async def _database():
    async with get_async_db() as db:
        dialect = db.bind.dialect.name
        try:
            if dialect == 'postgresql':
                size = await db.scalar(text('SELECT pg_database_size(current_database())'))
            elif dialect == 'sqlite':
                pages = await db.scalar(text('PRAGMA page_count'))
                page_size = await db.scalar(text('PRAGMA page_size'))
                size = (pages or 0) * (page_size or 0)
            else:
                size = None
        except Exception:
            log.exception('Could not measure the database size')
            size = None
    return {'dialect': dialect, 'size': size}


async def _sandbox(path):
    """Controller data, or {'available': False, 'error'} so the rest of the page still renders."""
    if not await Config.get('team.enabled', False) or not await sandbox_configured():
        return {'available': False, 'configured': False}
    try:
        return {'available': True, 'configured': True, **await admin_controller_request(path, timeout=30)}
    except HTTPException as error:
        return {'available': False, 'configured': True, 'error': str(error.detail)}


@router.get('/overview')
async def overview(user=Depends(get_admin_user)):
    host, database, sandbox = await asyncio.gather(
        asyncio.to_thread(_host), _database(), _sandbox('/admin/host')
    )
    return {
        'host': host,
        'database': database,
        'storage_provider': STORAGE_PROVIDER,
        'sandbox': sandbox,
        # Each WebUI replica reports itself; the browser reached this one.
        'generated_at': int(time.time()),
    }


@router.get('/storage/users')
async def storage_users(user=Depends(get_admin_user)):
    async with get_async_db() as db:
        members = (
            await db.execute(select(User.id, User.name, User.email, User.role).where(User.role != 'pending'))
        ).all()
        files = await file_usage_by_user(db)
        limits = await plan_limits(db=db)
    enforced = await limits_enabled()
    sandbox = await _sandbox('/admin/workspaces')
    sandbox_users = sandbox.get('users', {}) if sandbox.get('available') else {}
    rows = []
    for member in members:
        usage = files.get(member.id, {'used': 0, 'file_count': 0, 'unknown_size': 0})
        limit = limits.get(member.id, {})
        exempt = member.role == 'admin' or not enforced
        disk = sandbox_users.get(member.id)
        rows.append(
            {
                'id': member.id,
                'name': member.name,
                'email': member.email,
                'role': member.role,
                'plan_name': limit.get('plan_name'),
                'files': {**usage, 'limit': None if exempt else limit.get('storage_bytes')},
                'sandbox': {
                    'used': disk.get('used') if disk else None,
                    'over': bool(disk and disk.get('over')),
                    'limit': None if exempt else limit.get('sandbox_bytes'),
                },
            }
        )
    rows.sort(key=lambda row: -(row['files']['used'] + (row['sandbox']['used'] or 0)))
    return {
        'users': rows,
        'enforced': enforced,
        'total_files_bytes': sum(row['files']['used'] for row in rows),
        'sandbox_available': sandbox.get('available', False),
        'sandbox_scanned_at': sandbox.get('scanned_at'),
    }


@router.get('/sandboxes')
async def sandboxes(user=Depends(get_admin_user)):
    data = await _sandbox('/admin/workspaces')
    if not data.get('available'):
        return data
    items = data.get('workspaces', [])
    user_ids = {item['user_id'] for item in items}
    project_ids = {item['project_id'] for item in items if item['project_id'] != 'personal'}
    async with get_async_db() as db:
        names = dict(
            (await db.execute(select(User.id, User.name).where(User.id.in_(user_ids)))).all() if user_ids else []
        )
        projects = dict(
            (await db.execute(select(Folder.id, Folder.name).where(Folder.id.in_(project_ids)))).all()
            if project_ids
            else []
        )
    for item in items:
        item['user_name'] = names.get(item['user_id'])
        item['project_name'] = None if item['project_id'] == 'personal' else projects.get(item['project_id'])
    return data


class SandboxAction(BaseModel):
    action: str
    confirm: str | None = None


# Inbox copy for the member whose workspace an administrator changed. The keys are translated
# by the inbox (registered for extraction in src/lib/components/team/i18n-keys.ts).
NOTICES = {
    'stop': 'An administrator stopped your workspace "{{workspace}}". Your files are kept; it starts again when you use it.',
    'recreate': 'An administrator restarted your workspace "{{workspace}}" with a fresh runtime. Your files are kept.',
    'clear': 'An administrator cleared your workspace "{{workspace}}". Its files were deleted.',
}
PERSONAL_NOTICES = {
    'stop': 'An administrator stopped your personal workspace. Your files are kept; it starts again when you use it.',
    'recreate': 'An administrator restarted your personal workspace with a fresh runtime. Your files are kept.',
    'clear': 'An administrator cleared your personal workspace. Its files were deleted.',
}


@router.post('/sandboxes/{workspace_id}')
async def sandbox_action(workspace_id: str, form: SandboxAction, user=Depends(get_admin_user)):
    if form.action not in NOTICES:
        raise HTTPException(400, 'Unknown action')
    if not await sandbox_configured():
        raise HTTPException(503, 'The sandbox service is not configured')
    result = await admin_controller_request(
        f'/admin/workspaces/{workspace_id}', 'POST', form.model_dump(), timeout=120
    )
    owner_id, project_id = result.get('user_id'), result.get('project_id')
    forget_sandbox_disk(owner_id)
    async with get_async_db() as db:
        workspace = None
        if project_id and project_id != 'personal':
            workspace = await db.scalar(select(Folder.name).where(Folder.id == project_id)) or project_id
        key = NOTICES[form.action] if workspace else PERSONAL_NOTICES[form.action]
        params = {'workspace': workspace} if workspace else {}
        db.add(
            QuotaAudit(
                id=f'sandbox-{workspace_id}-{time.time_ns()}',
                user_id=owner_id,
                actor_id=user.id,
                action=f'sandbox_{form.action}',
                data={'workspace_id': workspace_id, 'project_id': project_id},
                created_at=int(time.time()),
            )
        )
        notified = set()
        if owner_id and owner_id != user.id:
            try:
                notified = await insert_notifications(
                    recipients={owner_id: 'system.announcement'},
                    event_key=f'sandbox:{workspace_id}:{form.action}:{time.time_ns()}',
                    data={
                        'message': key.replace('{{workspace}}', workspace or ''),
                        'key': key,
                        'params': params,
                    },
                    db=db,
                )
            except Exception:
                log.exception('Could not notify the workspace owner')
        await db.commit()
    if notified:
        await emit_changes(notified)
    log.info('Admin %s ran %s on sandbox workspace %s', user.id, form.action, workspace_id)
    return result

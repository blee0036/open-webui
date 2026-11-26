"""Projects reuse resource ACLs, never legacy folder conversation inheritance."""

import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from open_webui.events import EVENTS, publish_event
from open_webui.internal.db import get_async_db
from open_webui.models.access_grants import AccessGrants
from open_webui.models.chats import Chat
from open_webui.models.config import Config
from open_webui.models.files import Files
from open_webui.models.folders import Folder, FolderModel, Folders
from open_webui.models.team import QuotaAudit
from open_webui.utils.access_control import validate_access_grants
from open_webui.utils.access_control.files import has_access_to_file
from open_webui.utils.access_control.folders import has_folder_access
from open_webui.utils.auth import get_verified_user
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update

router = APIRouter()


async def authorized_project(project_id, user, permission='read', db=None):
    folder = await Folders.get_folder_by_id(project_id, db=db)
    if not folder or folder.kind != 'project' or not await has_folder_access(user.id, folder, permission, db):
        raise HTTPException(404, 'Project not found or access denied')
    return folder


class ProjectForm(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    system_prompt: str = Field(default='', max_length=100000)
    model_ids: list[str] = Field(default_factory=list, max_length=20)


@router.get('')
async def projects(user=Depends(get_verified_user)):
    async with get_async_db() as db:
        rows = (
            await db.execute(select(Folder).where(Folder.kind == 'project').order_by(Folder.updated_at.desc()))
        ).scalars()
        return [
            FolderModel.model_validate(row)
            for row in rows
            if await has_folder_access(user.id, FolderModel.model_validate(row), 'read', db)
        ]


@router.post('')
async def create_project(form: ProjectForm, request: Request, user=Depends(get_verified_user)):
    now = int(time.time())
    async with get_async_db() as db:
        row = Folder(
            id=str(uuid.uuid4()),
            name=form.name,
            kind='project',
            user_id=user.id,
            data={
                'system_prompt': form.system_prompt,
                'model_ids': form.model_ids,
                'files': [],
                'machine_id': 'managed',
                'working_directory': '/home/user',
            },
            created_at=now,
            updated_at=now,
            is_expanded=False,
        )
        db.add(row)
        await db.commit()
        result = FolderModel.model_validate(row)
    await publish_event(request, EVENTS.FOLDER_CREATED, actor=user, subject_id=result.id, data={'kind': 'project'})
    return result


@router.get('/{project_id}')
async def get_project(project_id: str, user=Depends(get_verified_user)):
    folder = await authorized_project(project_id, user)
    grants = await AccessGrants.get_grants_by_resource('folder', project_id)
    return {
        **folder.model_dump(),
        'write_access': await has_folder_access(user.id, folder, 'write', None),
        'access_grants': [g.model_dump() for g in grants],
    }


class ProjectSaveForm(ProjectForm):
    # context_revision the editor loaded; None skips the check (older clients).
    revision: int | None = None
    force: bool = False


@router.put('/{project_id}')
async def save_project(project_id: str, form: ProjectSaveForm, request: Request, user=Depends(get_verified_user)):
    await authorized_project(project_id, user, 'write')
    now = int(time.time())
    async with get_async_db() as db:
        async with db.begin():
            # Take the row write lock first, then re-read, so concurrent edits of
            # other keys (e.g. published files) are never overwritten.
            await db.execute(update(Folder).where(Folder.id == project_id).values(updated_at=now))
            row = await db.get(Folder, project_id, populate_existing=True)
            data = dict(row.data or {})
            current = data.get('context_revision', 0)
            if form.revision is not None and form.revision != current and not form.force:
                raise HTTPException(
                    409,
                    detail={
                        'code': 'project_conflict',
                        'message': 'Project context was changed by someone else after you opened it.',
                        'revision': current,
                        'updated_by': data.get('context_updated_by'),
                        'updated_at': data.get('context_updated_at'),
                    },
                )
            before = {
                'name': row.name,
                'system_prompt': data.get('system_prompt', ''),
                'model_ids': data.get('model_ids', []),
            }
            after = {'name': form.name, 'system_prompt': form.system_prompt, 'model_ids': form.model_ids}
            row.name = form.name
            row.data = {
                **data,
                **{k: v for k, v in after.items() if k != 'name'},
                'context_revision': current + 1,
                'context_updated_by': user.id,
                'context_updated_at': now,
            }
            db.add(
                QuotaAudit(
                    id=str(uuid.uuid4()),
                    user_id=row.user_id,
                    actor_id=user.id,
                    action='project_context_update',
                    data={
                        'project_id': project_id,
                        'revision': current + 1,
                        'base_revision': form.revision,
                        'overwrote_newer': form.revision is not None and form.revision != current,
                        'changes': {k: {'from': before[k], 'to': after[k]} for k in after if before[k] != after[k]},
                    },
                    created_at=now,
                )
            )
    await publish_event(request, EVENTS.FOLDER_UPDATED, actor=user, subject_id=project_id)
    return await get_project(project_id, user)


async def audit_project_file(db, project_id, owner_id, actor_id, action, file):
    db.add(
        QuotaAudit(
            id=str(uuid.uuid4()),
            user_id=owner_id,
            actor_id=actor_id,
            action=action,
            data={'project_id': project_id, 'file_id': file.get('id'), 'name': file.get('name')},
            created_at=int(time.time()),
        )
    )


class ProjectAccessForm(BaseModel):
    access_grants: list[dict] = Field(default_factory=list, max_length=500)


@router.put('/{project_id}/access')
async def project_access(project_id: str, form: ProjectAccessForm, request: Request, user=Depends(get_verified_user)):
    folder = await authorized_project(project_id, user)
    if folder.user_id != user.id:
        raise HTTPException(403, 'Only the project owner can manage membership')
    if any(
        g.get('principal_id') == '*' or g.get('principal_type') not in {'user', 'group'} for g in form.access_grants
    ):
        raise HTTPException(400, 'Invite specific users or groups')
    grants = await validate_access_grants(
        await Config.get('user.permissions'), user.id, user.role, form.access_grants, resource_type='folder'
    )
    await AccessGrants.set_access_grants('folder', project_id, grants)
    await publish_event(
        request, EVENTS.FOLDER_ACCESS_UPDATED, actor=user, subject_id=project_id, data={'grant_count': len(grants)}
    )
    return await get_project(project_id, user)


@router.get('/{project_id}/chats')
async def project_chats(project_id: str, user=Depends(get_verified_user)):
    await authorized_project(project_id, user)
    async with get_async_db() as db:
        rows = (
            await db.execute(
                select(Chat.id, Chat.title, Chat.user_id, Chat.visibility, Chat.updated_at)
                .where(
                    Chat.folder_id == project_id,
                    or_(Chat.user_id == user.id, Chat.visibility == 'project'),
                    Chat.archived.is_(False),
                    Chat.meta['internal'].as_boolean().is_not(True),
                )
                .order_by(Chat.updated_at.desc())
                .limit(200)
            )
        ).all()
        return [dict(row._mapping) for row in rows]


class VisibilityForm(BaseModel):
    visibility: Literal['private', 'project']


@router.put('/{project_id}/chats/{chat_id}/visibility')
async def visibility(
    project_id: str, chat_id: str, form: VisibilityForm, request: Request, user=Depends(get_verified_user)
):
    await authorized_project(project_id, user)
    async with get_async_db() as db:
        result = await db.execute(
            update(Chat)
            .where(Chat.id == chat_id, Chat.user_id == user.id, Chat.folder_id == project_id)
            .values(visibility=form.visibility)
        )
        if not result.rowcount:
            raise HTTPException(404, 'Only the conversation owner can share it')
        await db.commit()
    await publish_event(
        request,
        EVENTS.CHAT_SHARED if form.visibility == 'project' else EVENTS.CHAT_UNSHARED,
        actor=user,
        subject_id=chat_id,
        data={'visibility': form.visibility},
    )
    return {'visibility': form.visibility}


class PublishFileForm(BaseModel):
    file_id: str


@router.post('/{project_id}/files')
async def publish_file(project_id: str, form: PublishFileForm, user=Depends(get_verified_user)):
    await authorized_project(project_id, user, 'write')
    source = await Files.get_file_by_id(form.file_id)
    if not source or not await has_access_to_file(form.file_id, 'read', user):
        raise HTTPException(404, 'File unavailable')
    async with get_async_db() as db:
        await db.execute(update(Folder).where(Folder.id == project_id).values(updated_at=int(time.time())))
        row = await db.get(Folder, project_id, populate_existing=True)
        data = dict(row.data or {})
        files = [f for f in data.get('files', []) if f.get('id') != source.id]
        entry = {'id': source.id, 'type': 'file', 'name': source.filename, 'published_by': user.id}
        files.append(entry)
        row.data = {**data, 'files': files}
        await audit_project_file(db, project_id, row.user_id, user.id, 'project_file_publish', entry)
        await db.commit()
    return await get_project(project_id, user)


@router.delete('/{project_id}/files/{file_id}')
async def unpublish_file(project_id: str, file_id: str, user=Depends(get_verified_user)):
    await authorized_project(project_id, user, 'write')
    async with get_async_db() as db:
        await db.execute(update(Folder).where(Folder.id == project_id).values(updated_at=int(time.time())))
        row = await db.get(Folder, project_id, populate_existing=True)
        existing = (row.data or {}).get('files', [])
        removed = next((f for f in existing if f.get('id') == file_id), None)
        row.data = {
            **(row.data or {}),
            'files': [f for f in existing if f.get('id') != file_id],
        }
        if removed:
            await audit_project_file(db, project_id, row.user_id, user.id, 'project_file_unpublish', removed)
        await db.commit()
    return {'ok': True}

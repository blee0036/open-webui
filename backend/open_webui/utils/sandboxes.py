"""Trusted user × project adapter for the MIT Open Terminal lifecycle service."""

import json
from urllib.parse import quote

import aiohttp
from fastapi import HTTPException
from open_webui.models.chats import Chats
from open_webui.models.config import Config
from open_webui.models.folders import Folders
from open_webui.utils.access_control.folders import has_folder_access

MANAGED_TERMINAL_ID = 'managed'


async def workspace_scope(user, metadata=None):
    metadata = metadata or {}
    chat_id = metadata.get('chat_id')
    project_id = metadata.get('project_id')
    explicit_project = bool(project_id) or (isinstance(chat_id, str) and chat_id.startswith('project:'))
    if isinstance(chat_id, str) and chat_id.startswith('project:'):
        project_id = chat_id.removeprefix('project:')
    elif chat_id and not str(chat_id).startswith(('local:', 'temporary:', 'automation:')):
        chat = await Chats.get_chat_by_id(chat_id)
        if chat:
            # Shared chats never grant access to the author's execution environment.
            if chat.user_id != user.id:
                raise HTTPException(403, 'Only the conversation owner can open its execution workspace')
            project_id = chat.folder_id
    project_id = project_id or metadata.get('folder_id')
    if project_id:
        folder = await Folders.get_folder_by_id(project_id)
        if folder and folder.kind == 'project':
            if not await has_folder_access(user.id, folder, 'read', None):
                raise HTTPException(403, 'Project workspace is unavailable')
            return folder.id
        if explicit_project:
            raise HTTPException(404, 'Project workspace not found')
    return 'personal'


async def sandbox_configured() -> bool:
    return bool(str(await Config.get('team.sandbox.url') or '').strip() and await Config.get('team.sandbox.key'))


async def controller_config():
    url = str(await Config.get('team.sandbox.url') or '').rstrip('/')
    key = await Config.get('team.sandbox.key')
    if not url or not key:
        raise HTTPException(
            503, 'Managed workspace is not configured. Ask an administrator to deploy the sandbox service.'
        )
    return url, key


async def managed_connection(user):
    url, key = await controller_config()
    return {
        'id': MANAGED_TERMINAL_ID,
        'name': 'My managed workspace',
        'owner_id': user.id,
        'url': f'{url}/r/{quote(user.id, safe="")}/personal',
        'auth_type': 'bearer',
        'key': key,
        'enabled': True,
        'managed': True,
        'config': {
            'chat_uploads': 'filesystem',
            'access_grants': [{'principal_type': 'user', 'principal_id': user.id, 'permission': 'read'}],
        },
    }


async def bind_managed_connection(connection, user, metadata=None):
    if not connection.get('managed'):
        return connection
    project = await workspace_scope(user, metadata)
    url, key = await controller_config()
    return {**connection, 'url': f'{url}/r/{quote(user.id, safe="")}/{quote(project, safe="")}', 'key': key}


async def controller_request(user, project, method='GET', body=None):
    scope = await workspace_scope(user, {'project_id': project} if project != 'personal' else {})
    url, key = await controller_config()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300), trust_env=False) as session:
        try:
            async with session.request(
                method,
                f'{url}/workspaces/{quote(user.id, safe="")}/{quote(scope, safe="")}',
                json=body,
                headers={'Authorization': f'Bearer {key}'},
                allow_redirects=False,
            ) as response:
                data = await response.json()
                if response.status >= 400:
                    raise HTTPException(response.status, data.get('detail', 'Workspace operation failed'))
                return data
        except (aiohttp.ClientError, TimeoutError) as error:
            raise HTTPException(503, 'Workspace controller is unavailable; existing files are retained') from error


async def sandbox_python(user, metadata, payload):
    scope = await workspace_scope(user, metadata)
    url, key = await controller_config()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300), trust_env=False) as session:
        try:
            async with session.post(
                f'{url}/tools/{quote(user.id, safe="")}/{quote(scope, safe="")}',
                json={**payload, 'user_id': user.id},
                headers={'Authorization': f'Bearer {key}'},
                allow_redirects=False,
            ) as response:
                data = await response.json()
                if response.status >= 400:
                    raise HTTPException(response.status, data.get('detail', 'Sandbox tool failed'))
                return data
        except (aiohttp.ClientError, TimeoutError) as error:
            raise HTTPException(
                503, 'Sandbox tool did not complete. Files are retained; no shared fallback was used.'
            ) from error


async def sandbox_execute_code(user, metadata, code):
    """Stage code as JSON data, then run it through the workspace's Agent queue."""
    source = """import contextlib, io, traceback
class Tools:
    def run(self, code: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(compile(code, "<workspace>", "exec"), {"__name__": "__main__"})
            except Exception:
                traceback.print_exc()
        return {"stdout": out.getvalue(), "stderr": err.getvalue(), "result": ""}
"""
    return await sandbox_python(
        user,
        metadata,
        {
            'action': 'execute',
            'tool_id': 'workspace_python',
            'content': source,
            'function': 'run',
            'params': {'code': code},
        },
    )


def schema_valves(schema):
    from pydantic import BaseModel, ConfigDict

    class SandboxValves(BaseModel):
        model_config = ConfigDict(extra='allow')

        @classmethod
        def model_json_schema(cls, **kwargs):
            return schema

        @classmethod
        def schema(cls, **kwargs):
            return schema

    return SandboxValves


class SandboxTool:
    """Data-only descriptor: construction and validation never execute user source."""

    def __init__(self, discovery):
        self._sandbox_discovery = discovery
        self._sandbox_specs = discovery['specs']
        if discovery.get('valves_schema'):
            self.Valves = schema_valves(discovery['valves_schema'])
        if discovery.get('user_valves_schema'):
            self.UserValves = schema_valves(discovery['user_valves_schema'])


async def requires_sandbox(tool):
    if getattr(tool.meta, 'runtime', None) == 'sandbox':
        return True
    if await Config.get('team.enabled', False):
        from open_webui.models.users import Users

        owner = await Users.get_user_by_id(tool.user_id)
        return not owner or owner.role != 'admin'
    return False


async def sandbox_tool_descriptor(tool_id, content, user_id):
    from open_webui.models.users import Users

    user = await Users.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, 'Tool owner not found')
    result = await sandbox_python(user, {}, {'action': 'discover', 'tool_id': tool_id, 'content': content})
    descriptor = SandboxTool(result)
    return descriptor, {'runtime': 'sandbox'}, None


async def sandbox_tool_functions(tool, user, metadata):
    from open_webui.models.tools import Tools

    result = {}
    for spec in tool.specs:
        name = spec['name']

        def make_callable(function_name):
            async def execute(**params):
                if not await Tools.is_tool_active(tool.id):
                    raise HTTPException(403, 'Tool is disabled')
                if tool.user_id != user.id:
                    from open_webui.models.access_grants import AccessGrants

                    if not await AccessGrants.has_access(
                        user_id=user.id, resource_type='tool', resource_id=tool.id, permission='read'
                    ):
                        raise HTTPException(403, 'Tool sharing was revoked')
                # Do not copy toolkit author valves or credentials to another member.
                valves = await Tools.get_tool_valves_by_id(tool.id) if tool.user_id == user.id else {}
                personal = await Tools.get_user_valves_by_id_and_user_id(tool.id, user.id)
                value = await sandbox_python(
                    user,
                    metadata,
                    {
                        'action': 'execute',
                        'tool_id': tool.id,
                        'content': tool.content,
                        'function': function_name,
                        'params': params,
                        'valves': valves or {},
                        'user_valves': personal or {},
                    },
                )
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

            return execute

        result[name] = {
            'tool_id': tool.id,
            'callable': make_callable(name),
            'spec': spec,
            'type': 'sandbox',
            'metadata': {'runtime': 'sandbox'},
        }
    return result

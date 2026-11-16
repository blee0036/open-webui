"""Owner-authenticated MCP configurations. Credentials never enter shared resources."""

import re

import httpx2
from fastapi import HTTPException
from open_webui.env import ENABLE_TOOL_SERVERS
from open_webui.internal.db import get_async_db
from open_webui.models.config import Config
from open_webui.models.team import PersonalConnection
from open_webui.utils.access_control import has_permission
from open_webui.utils.team_secrets import unseal
from sqlalchemy import select

SERVER_ID = re.compile(r'^personal-([0-9a-f]{32})-([0-9a-f]{32})$')


async def can_manage_connections(user):
    if not ENABLE_TOOL_SERVERS or (
        user.role != 'admin'
        and not await has_permission(user.id, 'features.direct_tool_servers', await Config.get('user.permissions'))
    ):
        raise HTTPException(403, 'Personal tool connections are disabled')


def connection_config(row):
    secret = unseal(row.secret, 'personal_connection')
    return {
        'type': 'mcp',
        'url': row.url,
        'auth_type': row.config.get('auth_type', 'none'),
        'key': secret.get('key', ''),
        'headers': secret.get('headers', {}),
        'personal_owner_id': row.user_id,
        'info': {
            'id': row.config['server_id'],
            'name': row.name,
            'oauth_client_info': secret.get('oauth_client_info', ''),
        },
        'config': {
            'enable': row.enabled,
            'access_grants': [{'principal_type': 'user', 'principal_id': row.user_id, 'permission': 'read'}],
        },
    }


async def personal_mcp_connections(user):
    try:
        await can_manage_connections(user)
    except HTTPException:
        return []
    async with get_async_db() as db:
        return [
            connection_config(row)
            for row in (
                await db.execute(
                    select(PersonalConnection).where(
                        PersonalConnection.user_id == user.id,
                        PersonalConnection.type == 'mcp',
                        PersonalConnection.enabled.is_(True),
                    )
                )
            ).scalars()
        ]


async def personal_mcp_connection(server_id, user_id=None):
    match = SERVER_ID.fullmatch(server_id)
    if not match:
        raise HTTPException(404, 'Personal MCP connection not found')
    async with get_async_db() as db:
        row = await db.get(PersonalConnection, match[1])
        if (
            not row
            or not row.enabled
            or row.type != 'mcp'
            or row.config.get('server_id') != server_id
            or (user_id is not None and row.user_id != user_id)
        ):
            raise HTTPException(404, 'Personal MCP connection not found')
        return connection_config(row)


async def verify_personal_oauth(client_id, user_id=None):
    if client_id.startswith('mcp:personal-'):
        return await personal_mcp_connection(client_id.removeprefix('mcp:'), user_id)
    return None


class PersonalMCPAuth(httpx2.Auth):
    """Recheck ownership/revision and refresh credentials for every MCP HTTP request."""

    def __init__(self, server_id, user, request):
        self.server_id, self.user, self.request = server_id, user, request

    async def async_auth_flow(self, request):
        from open_webui.utils.tools import build_tool_server_headers

        await can_manage_connections(self.user)
        connection = await personal_mcp_connection(self.server_id, self.user.id)
        headers, _ = await build_tool_server_headers(connection, self.request, self.user, self.server_id)
        if connection['auth_type'] in {'oauth_2.1', 'oauth_2.1_static'} and not headers.get('Authorization'):
            raise HTTPException(401, 'Connect your personal MCP account before using this tool')
        request.headers.update(headers)
        yield request

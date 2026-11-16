"""Personal, server-side MCP connections; existing direct OpenAPI/terminals stay intact."""

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from open_webui.internal.db import get_async_db
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.team import PersonalConnection
from open_webui.utils.auth import get_verified_user
from open_webui.utils.personal_connections import can_manage_connections
from open_webui.utils.team_network import personal_oauth, validate_endpoint
from open_webui.utils.team_secrets import seal, unseal
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select

router = APIRouter()


class ConnectionForm(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(max_length=2048)
    auth_type: str = 'none'
    key: str | None = Field(default=None, max_length=8192)
    headers: dict[str, str] | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_scope: str | None = None
    enabled: bool = True
    clear_credentials: bool = False

    @field_validator('url')
    @classmethod
    def valid_url(cls, value):
        return validate_endpoint(value.rstrip('/'))

    @field_validator('auth_type')
    @classmethod
    def valid_auth(cls, value):
        if value not in {'none', 'bearer', 'oauth_2.1', 'oauth_2.1_static'}:
            raise ValueError('Use none, bearer or OAuth authentication')
        return value

    @field_validator('headers')
    @classmethod
    def valid_headers(cls, value):
        if value and (
            len(value) > 30
            or any(
                len(k) > 200
                or len(v) > 8192
                or any(c in k + v for c in '\r\n')
                or k.lower()
                in {'host', 'cookie', 'connection', 'content-length', 'transfer-encoding', 'proxy-authorization'}
                for k, v in value.items()
            )
        ):
            raise ValueError('Invalid custom headers')
        return value


def public(row):
    secret = unseal(row.secret, 'personal_connection')
    return {
        'id': row.id,
        'name': row.name,
        'url': row.url,
        'enabled': row.enabled,
        'auth_type': row.config.get('auth_type', 'none'),
        'server_id': row.config['server_id'],
        'has_key': bool(secret.get('key')),
        'header_names': list(secret.get('headers', {})),
        'has_oauth': bool(secret.get('oauth_client_info')),
    }


@router.get('')
async def connections(user=Depends(get_verified_user)):
    await can_manage_connections(user)
    async with get_async_db() as db:
        return [
            public(row)
            for row in (
                await db.execute(
                    select(PersonalConnection)
                    .where(PersonalConnection.user_id == user.id)
                    .order_by(PersonalConnection.created_at)
                )
            ).scalars()
        ]


@router.put('/{connection_id}')
async def save_connection(connection_id: str, form: ConnectionForm, request: Request, user=Depends(get_verified_user)):
    await can_manage_connections(user)
    try:
        if uuid.UUID(hex=connection_id).hex != connection_id:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, 'Connection ID must be a lowercase UUID without hyphens') from None
    async with get_async_db() as db:
        row = await db.get(PersonalConnection, connection_id)
        if row and row.user_id != user.id:
            raise HTTPException(404, 'Connection not found')
        if (
            not row
            and (
                await db.execute(
                    select(func.count()).select_from(PersonalConnection).where(PersonalConnection.user_id == user.id)
                )
            ).scalar_one()
            >= 50
        ):
            raise HTTPException(400, 'A maximum of 50 personal connections is supported')
        secret = unseal(row.secret, 'personal_connection') if row and not form.clear_credentials else {}
        old_server = row.config['server_id'] if row else None
        # An edit has a new OAuth identity, so disabled/deleted/reconfigured callbacks
        # and caches on other workers cannot revive the old credentials.
        server_id = f'personal-{connection_id}-{uuid.uuid4().hex}'
        if form.key is not None:
            secret['key'] = form.key
        if form.headers is not None:
            secret['headers'] = form.headers
        if form.auth_type.startswith('oauth_'):
            from open_webui.utils.oauth import (
                encrypt_data,
                get_oauth_client_info_with_dynamic_client_registration,
                get_oauth_client_info_with_static_credentials,
            )

            token = personal_oauth.set(True)
            try:
                client_id = f'mcp:{server_id}'
                if form.auth_type == 'oauth_2.1_static':
                    app_id = form.oauth_client_id or secret.get('oauth_client_id')
                    app_secret = form.oauth_client_secret or secret.get('oauth_client_secret')
                    if not app_id or not app_secret:
                        raise HTTPException(400, 'OAuth client ID and secret are required')
                    secret.update(oauth_client_id=app_id, oauth_client_secret=app_secret)
                    info = await get_oauth_client_info_with_static_credentials(
                        request, client_id, form.url, app_id, app_secret, form.oauth_scope
                    )
                else:
                    info = await get_oauth_client_info_with_dynamic_client_registration(
                        request, client_id, form.url, oauth_scope=form.oauth_scope
                    )
                secret['oauth_client_info'] = encrypt_data(info.model_dump(mode='json'))
            except HTTPException:
                raise
            except Exception as error:
                raise HTTPException(
                    400, 'MCP OAuth registration failed. Check the endpoint and client configuration.'
                ) from error
            finally:
                personal_oauth.reset(token)
        else:
            secret.pop('oauth_client_info', None)
        now = int(time.time())
        if not row:
            row = PersonalConnection(id=connection_id, user_id=user.id, type='mcp', created_at=now)
            db.add(row)
        row.name, row.url, row.enabled, row.updated_at = form.name, form.url, form.enabled, now
        row.secret = seal(secret, 'personal_connection')
        row.config = {'auth_type': form.auth_type, 'server_id': server_id}
        await db.commit()
        value = public(row)
    if old_server:
        request.app.state.oauth_client_manager.remove_client(f'mcp:{old_server}')
        await OAuthSessions.delete_sessions_by_user_id_and_provider(user.id, f'mcp:{old_server}')
    return value


@router.delete('/{connection_id}')
async def remove_connection(connection_id: str, request: Request, user=Depends(get_verified_user)):
    await can_manage_connections(user)
    async with get_async_db() as db:
        row = await db.get(PersonalConnection, connection_id)
        if not row or row.user_id != user.id:
            raise HTTPException(404, 'Connection not found')
        server_id = row.config['server_id']
        await db.execute(
            delete(PersonalConnection).where(
                PersonalConnection.id == connection_id, PersonalConnection.user_id == user.id
            )
        )
        await db.commit()
    request.app.state.oauth_client_manager.remove_client(f'mcp:{server_id}')
    await OAuthSessions.delete_sessions_by_user_id_and_provider(user.id, f'mcp:{server_id}')
    return {'status': True}

"""Lark/Feishu custom-app authorization-code callbacks, independent of legacy OAuth."""

import secrets
import time
import uuid
from urllib.parse import urlencode, urlsplit

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from open_webui.env import WEBUI_AUTH_COOKIE_SAME_SITE, WEBUI_AUTH_COOKIE_SECURE
from open_webui.internal.db import get_async_db
from open_webui.models.auths import Auth, AuthModel
from open_webui.models.team import (
    EnterpriseConnection,
    EnterpriseIdentity,
    EnterpriseLoginState,
    QuotaAccount,
    QuotaAudit,
)
from open_webui.models.users import User, UserModel
from open_webui.models.config import Config
from open_webui.utils.auth import create_signin_response, get_admin_user, get_password_hash
from open_webui.utils.quotas import record
from open_webui.utils.team_secrets import seal, unseal
from open_webui.utils.groups import apply_default_group_assignment
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select, update

router = APIRouter()
PLATFORMS = {
    'feishu': ('https://accounts.feishu.cn', 'https://open.feishu.cn'),
    'lark': ('https://accounts.larksuite.com', 'https://open.larksuite.com'),
}


class ConnectionForm(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    platform: str
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str | None = Field(default=None, max_length=2048)
    redirect_uri: str
    tenant_ids: list[str] = Field(min_length=1, max_length=100)
    scope: str = Field(default='contact:user.base:readonly', max_length=2000)
    enabled: bool = True

    @field_validator('platform')
    @classmethod
    def valid_platform(cls, value):
        if value not in PLATFORMS:
            raise ValueError('Platform must be feishu or lark')
        return value

    @field_validator('redirect_uri')
    @classmethod
    def valid_redirect(cls, value):
        uri = urlsplit(value)
        if not uri.hostname or uri.username or uri.password or uri.fragment or uri.query:
            raise ValueError('Callback must be an absolute URL without credentials, query or fragment')
        if uri.scheme != 'https' and not (uri.scheme == 'http' and uri.hostname in {'localhost', '127.0.0.1', '::1'}):
            raise ValueError('Callback must use HTTPS (localhost HTTP is allowed for development)')
        return value

    @field_validator('tenant_ids')
    @classmethod
    def valid_tenants(cls, values):
        values = list(dict.fromkeys(value.strip() for value in values))
        if not values or any(not value or value == '*' or len(value) > 200 for value in values):
            raise ValueError('List explicit allowed enterprise tenant keys')
        return values


def public_connection(row):
    value = record(row)
    value.pop('secret')
    value['has_secret'] = bool(row.secret)
    return value


@router.get('/connections')
async def login_connections():
    async with get_async_db() as db:
        rows = (
            await db.execute(
                select(EnterpriseConnection)
                .where(EnterpriseConnection.enabled.is_(True))
                .order_by(EnterpriseConnection.name)
            )
        ).scalars()
        return [{'id': row.id, 'name': row.name, 'platform': row.platform} for row in rows]


@router.get('/admin/connections')
async def connections(user=Depends(get_admin_user)):
    async with get_async_db() as db:
        return [
            public_connection(row)
            for row in (await db.execute(select(EnterpriseConnection).order_by(EnterpriseConnection.name))).scalars()
        ]


@router.put('/admin/connections/{connection_id}')
async def save_connection(connection_id: str, form: ConnectionForm, request: Request, user=Depends(get_admin_user)):
    if not connection_id or len(connection_id) > 80 or not all(c.isalnum() or c in '-_' for c in connection_id):
        raise HTTPException(400, 'Connection ID must contain only letters, numbers, hyphens or underscores')
    if urlsplit(form.redirect_uri).path.rstrip('/') != f'/api/v1/enterprise/{connection_id}/callback':
        raise HTTPException(400, f'Callback path must be /api/v1/enterprise/{connection_id}/callback')
    changed_users = []
    async with get_async_db() as db:
        row = await db.get(EnterpriseConnection, connection_id)
        now = int(time.time())
        if not row:
            if not form.client_secret:
                raise HTTPException(400, 'App Secret is required for a new connection')
            row = EnterpriseConnection(id=connection_id, created_at=now, revision=0)
            db.add(row)
        else:
            changed_users = list(
                (
                    await db.execute(
                        select(EnterpriseIdentity.user_id).where(EnterpriseIdentity.connection_id == connection_id)
                    )
                ).scalars()
            )
        for key, value in form.model_dump(exclude={'client_secret'}).items():
            setattr(row, key, value)
        if form.client_secret:
            row.secret = seal({'client_secret': form.client_secret}, 'enterprise')
        row.revision += 1
        row.updated_at = now
        db.add(
            QuotaAudit(
                id=str(uuid.uuid4()),
                actor_id=user.id,
                action='enterprise_connection',
                data={'connection_id': row.id, 'enabled': row.enabled, 'revision': row.revision},
                created_at=now,
            )
        )
        # Updating a connection invalidates callbacks and sessions issued under its old configuration.
        if changed_users:
            await db.execute(update(Auth).where(Auth.id.in_(changed_users)).values(session_stamp=str(uuid.uuid4())))
        await db.commit()
        result = public_connection(row)
    if changed_users:
        from open_webui.socket.main import disconnect_user_sessions

        for user_id in set(changed_users):
            await disconnect_user_sessions(user_id)
    return result


@router.get('/admin/identities')
async def identities(connection_id: str | None = None, user=Depends(get_admin_user)):
    async with get_async_db() as db:
        stmt = select(EnterpriseIdentity).order_by(EnterpriseIdentity.id).limit(500)
        if connection_id:
            stmt = stmt.where(EnterpriseIdentity.connection_id == connection_id)
        return [record(row) for row in (await db.execute(stmt)).scalars()]


class IdentityLinkForm(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


@router.put('/admin/connections/{connection_id}/identity')
async def link_identity(connection_id: str, form: IdentityLinkForm, user=Depends(get_admin_user)):
    """Explicitly bind a verified enterprise subject to an existing account; never match email."""
    changed = {form.user_id}
    async with get_async_db() as db:
        async with db.begin():
            connection = await db.get(EnterpriseConnection, connection_id)
            if not connection or form.tenant_id not in connection.tenant_ids:
                raise HTTPException(400, 'Choose a connection and one of its allowed tenant keys')
            # Serialize against login binding and connection changes on SQLite and PostgreSQL.
            await db.execute(
                update(EnterpriseConnection)
                .where(EnterpriseConnection.id == connection_id)
                .values(updated_at=EnterpriseConnection.updated_at)
            )
            target = await db.get(User, form.user_id)
            auth = await db.get(Auth, form.user_id)
            if not target or not auth or not auth.active or target.role not in {'user', 'admin'}:
                raise HTTPException(400, 'Choose an active existing account')
            row = (
                await db.execute(
                    select(EnterpriseIdentity).where(
                        EnterpriseIdentity.connection_id == connection_id,
                        EnterpriseIdentity.tenant_id == form.tenant_id,
                        EnterpriseIdentity.subject == form.subject,
                    )
                )
            ).scalar_one_or_none()
            previous = row.user_id if row else None
            if row:
                changed.add(row.user_id)
                row.user_id = target.id
            else:
                db.add(
                    EnterpriseIdentity(
                        id=str(uuid.uuid4()),
                        connection_id=connection_id,
                        tenant_id=form.tenant_id,
                        subject=form.subject,
                        user_id=target.id,
                        profile={},
                    )
                )
            await db.execute(update(Auth).where(Auth.id.in_(changed)).values(session_stamp=str(uuid.uuid4())))
            db.add(
                QuotaAudit(
                    id=str(uuid.uuid4()),
                    actor_id=user.id,
                    user_id=target.id,
                    action='enterprise_identity_link',
                    data={
                        'connection_id': connection_id,
                        'tenant_id': form.tenant_id,
                        'subject': form.subject,
                        'previous_user_id': previous,
                        'reason': form.reason,
                    },
                    created_at=int(time.time()),
                )
            )
    from open_webui.socket.main import disconnect_user_sessions

    for user_id in changed:
        await disconnect_user_sessions(user_id)
    return {'user_id': form.user_id}


@router.get('/{connection_id}/login')
async def login(connection_id: str):
    async with get_async_db() as db:
        connection = await db.get(EnterpriseConnection, connection_id)
        if not connection or not connection.enabled:
            raise HTTPException(404, 'Enterprise login is unavailable')
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        await db.execute(delete(EnterpriseLoginState).where(EnterpriseLoginState.expires_at < now))
        db.add(
            EnterpriseLoginState(
                id=state,
                connection_id=connection_id,
                revision=connection.revision,
                browser_nonce=nonce,
                expires_at=now + 300,
            )
        )
        await db.commit()
        accounts, _ = PLATFORMS[connection.platform]
        params = {
            'client_id': connection.client_id,
            'response_type': 'code',
            'redirect_uri': connection.redirect_uri,
            'state': state,
            'scope': connection.scope,
        }
        response = RedirectResponse(f'{accounts}/open-apis/authen/v1/authorize?{urlencode(params)}', status_code=302)
        response.set_cookie(
            f'enterprise_{connection_id}',
            nonce,
            max_age=300,
            httponly=True,
            secure=WEBUI_AUTH_COOKIE_SECURE,
            samesite='lax',
            path=f'/api/v1/enterprise/{connection_id}/callback',
        )
        response.headers['Cache-Control'] = 'no-store'
        return response


async def exchange_identity(connection, code):
    _, api = PLATFORMS[connection['platform']]
    secret = unseal(connection['secret'], 'enterprise')['client_secret']
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=True) as client:
        async with client.post(
            f'{api}/open-apis/authen/v2/oauth/token',
            json={
                'grant_type': 'authorization_code',
                'client_id': connection['client_id'],
                'client_secret': secret,
                'redirect_uri': connection['redirect_uri'],
                'code': code,
            },
            allow_redirects=False,
        ) as response:
            data = await response.json()
            if response.status != 200 or data.get('code', 0) != 0 or not data.get('access_token'):
                raise HTTPException(400, 'Enterprise authorization failed. Check the app configuration and try again.')
        async with client.get(
            f'{api}/open-apis/authen/v1/user_info',
            headers={'Authorization': f'Bearer {data["access_token"]}'},
            allow_redirects=False,
        ) as response:
            data = await response.json()
            if response.status != 200 or data.get('code', 0) != 0 or not isinstance(data.get('data'), dict):
                raise HTTPException(400, 'Enterprise user information is unavailable')
    return data['data']


async def bind_identity(connection, profile):
    tenant = str(profile.get('tenant_key') or '')
    subject = str(profile.get('open_id') or '')
    if not subject or not tenant or tenant not in connection['tenant_ids']:
        raise HTTPException(403, 'This account does not belong to an allowed enterprise')
    # Compute the unused password before taking a database write lock.
    password = await get_password_hash(secrets.token_urlsafe(48))
    # The tenant allowlist is the signup gate, so ENABLE_SIGNUP does not apply here.
    # A new account's role still follows the admin's default-role policy (never admin).
    role = await Config.get('ui.default_user_role') or 'pending'
    if role not in {'pending', 'user'}:
        role = 'pending'
    created = False
    async with get_async_db() as db:
        async with db.begin():
            result = await db.execute(
                update(EnterpriseConnection)
                .where(
                    EnterpriseConnection.id == connection['id'],
                    EnterpriseConnection.enabled.is_(True),
                    EnterpriseConnection.revision == connection['revision'],
                )
                .values(updated_at=EnterpriseConnection.updated_at)
            )
            if result.rowcount != 1:
                raise HTTPException(403, 'Enterprise login configuration changed. Please sign in again.')
            identity = (
                await db.execute(
                    select(EnterpriseIdentity).where(
                        EnterpriseIdentity.connection_id == connection['id'],
                        EnterpriseIdentity.tenant_id == tenant,
                        EnterpriseIdentity.subject == subject,
                    )
                )
            ).scalar_one_or_none()
            if identity:
                user = await db.get(User, identity.user_id)
                auth = await db.get(Auth, identity.user_id)
                # 'pending' still signs in so the user sees the awaiting-approval screen.
                if not user or not auth or not auth.active or user.role not in {'pending', 'user', 'admin'}:
                    raise HTTPException(403, 'This account is disabled')
                identity.profile = {
                    key: profile[key] for key in ('name', 'email', 'enterprise_email') if key in profile
                }
            else:
                created = True
                user_id = str(uuid.uuid4())
                now = int(time.time())
                # Internal unique address: no employee email permission and no email-based merging required.
                email = f'{user_id}@sso.identity.invalid'
                user = User(
                    id=user_id,
                    email=email,
                    name=str(profile.get('name') or profile.get('en_name') or 'Member')[:200],
                    role=role,
                    profile_image_url='/user.png',
                    oauth={},
                    info={'enterprise_email': profile.get('enterprise_email') or profile.get('email')},
                    last_active_at=now,
                    updated_at=now,
                    created_at=now,
                )
                db.add(user)
                auth = Auth(id=user_id, email=email, password=password, active=True, session_stamp=str(uuid.uuid4()))
                db.add(auth)
                db.add(QuotaAccount(user_id=user_id, pending_inherit=False))
                db.add(
                    EnterpriseIdentity(
                        id=str(uuid.uuid4()),
                        connection_id=connection['id'],
                        tenant_id=tenant,
                        subject=subject,
                        user_id=user_id,
                        profile={key: profile[key] for key in ('name', 'email', 'enterprise_email') if key in profile},
                    )
                )
                db.add(
                    QuotaAudit(
                        id=str(uuid.uuid4()),
                        actor_id=user_id,
                        user_id=user_id,
                        action='enterprise_user_created',
                        data={'connection_id': connection['id'], 'tenant_id': tenant, 'subject': subject, 'role': role},
                        created_at=now,
                    )
                )
                await db.flush()
            await db.flush()
            # Preserve the credential that authorized this callback. A concurrent disable/link
            # must not be able to issue a new session using its replacement stamp.
            result = UserModel.model_validate(user), AuthModel.model_validate(auth)
    # Group hierarchy owns its transaction; never acquire its SQLite write lock
    # while the identity transaction is still open.
    if created:
        await apply_default_group_assignment(await Config.get('ui.default_group_id'), result[0].id)
    return result


async def complete_callback(
    connection_id: str, request: Request, state: str = '', code: str = '', error: str | None = None
):
    if error or not state or not code or len(state) > 128 or len(code) > 4096:
        raise HTTPException(400, 'Enterprise authorization was cancelled or returned an invalid callback')
    async with get_async_db() as db:
        row = await db.get(EnterpriseLoginState, state)
        connection = await db.get(EnterpriseConnection, connection_id)
        cookie = request.cookies.get(f'enterprise_{connection_id}', '')
        if (
            not row
            or not connection
            or not connection.enabled
            or row.connection_id != connection_id
            or row.expires_at <= int(time.time())
            or row.revision != connection.revision
            or not secrets.compare_digest(row.browser_nonce, cookie)
        ):
            raise HTTPException(400, 'Enterprise login expired or did not originate in this browser. Please try again.')
        removed = await db.execute(delete(EnterpriseLoginState).where(EnterpriseLoginState.id == state))
        if removed.rowcount != 1:
            raise HTTPException(400, 'Enterprise login callback was already used')
        value = record(connection)
        await db.commit()
    profile = await exchange_identity(value, code)
    user, auth = await bind_identity(value, profile)
    result = await create_signin_response(
        request, user, source='oauth', auth=auth, provider=f'enterprise:{connection_id}'
    )
    response = RedirectResponse('/auth', status_code=302)
    response.headers['Cache-Control'] = 'no-store'
    response.delete_cookie(f'enterprise_{connection_id}', path=f'/api/v1/enterprise/{connection_id}/callback')
    if result.get('challenge_token'):
        from open_webui.routers.mfa import CHALLENGE_COOKIE

        response.set_cookie(
            CHALLENGE_COOKIE,
            result['challenge_token'],
            max_age=300,
            httponly=True,
            path='/api/v1/auths/mfa',
            secure=WEBUI_AUTH_COOKIE_SECURE,
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
        )
        response.headers['location'] = '/auth?mfa=1'
    else:
        response.set_cookie(
            'token',
            result['token'],
            httponly=False,
            secure=WEBUI_AUTH_COOKIE_SECURE,
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
            max_age=max(1, result['expires_at'] - int(time.time())) if result.get('expires_at') else None,
        )
    return response


@router.get('/{connection_id}/callback')
async def callback(connection_id: str, request: Request, state: str = '', code: str = '', error: str | None = None):
    try:
        return await complete_callback(connection_id, request, state, code, error)
    except (HTTPException, aiohttp.ClientError, TimeoutError, ValueError) as failure:
        message = (
            failure.detail
            if isinstance(failure, HTTPException) and isinstance(failure.detail, str)
            else 'Enterprise login could not be completed. Please try again or contact an administrator.'
        )
        response = RedirectResponse('/auth?' + urlencode({'error': message}), status_code=302)
        response.headers['Cache-Control'] = 'no-store'
        response.delete_cookie(f'enterprise_{connection_id}', path=f'/api/v1/enterprise/{connection_id}/callback')
        return response

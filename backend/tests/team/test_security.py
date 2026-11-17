import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from fastapi import HTTPException, Request
from open_webui.internal.db import get_async_db
from open_webui.models.access_grants import AccessGrants
from open_webui.models.auths import Auth
from open_webui.models.chats import Chat, Chats
from open_webui.models.config import Config
from open_webui.models.folders import Folder
from open_webui.models.team import EnterpriseConnection, EnterpriseLoginState
from open_webui.models.users import User, UserModel
from open_webui.routers import enterprise_sso as sso, personal_connections as connections, projects
from open_webui.utils.access_control import has_permission
from open_webui.utils.personal_connections import personal_mcp_connection
from open_webui.utils.sandboxes import SandboxTool, workspace_scope
from open_webui.utils.team_network import member_http_transport, oauth_http_session, resolved_address, validate_endpoint
from open_webui.utils.team_secrets import seal


def request(cookie=''):
    app = SimpleNamespace(state=SimpleNamespace(oauth_client_manager=SimpleNamespace(remove_client=lambda _: None)))
    return Request(
        {
            'type': 'http',
            'method': 'GET',
            'scheme': 'http',
            'path': '/',
            'headers': [(b'cookie', cookie.encode())],
            'app': app,
            'query_string': b'',
            'server': ('testserver', 80),
        }
    )


async def member(identifier, role='user'):
    async with get_async_db() as db:
        row = User(
            id=identifier,
            name=identifier,
            email=identifier + '@test.invalid',
            role=role,
            last_active_at=0,
            created_at=0,
            updated_at=0,
        )
        db.add(row)
        db.add(Auth(id=identifier, email=row.email, password='unused', active=True, session_stamp='original'))
        await db.commit()
        return UserModel.model_validate(row)


async def connection(identifier='company'):
    async with get_async_db() as db:
        row = EnterpriseConnection(
            id=identifier,
            name=identifier,
            platform='feishu',
            client_id='app-id',
            secret=seal({'client_secret': 'test-secret'}, 'enterprise'),
            redirect_uri=f'https://webui.test/api/v1/enterprise/{identifier}/callback',
            tenant_ids=['tenant'],
            scope='contact:user.base:readonly',
            enabled=True,
            revision=1,
            created_at=0,
            updated_at=0,
        )
        db.add(row)
        await db.commit()
        from open_webui.utils.quotas import record

        return record(row)


@pytest.mark.asyncio
async def test_sso_identity_not_email_and_wrong_tenant(monkeypatch):
    monkeypatch.setattr(sso, 'get_password_hash', AsyncMock(return_value='unused'))
    one = await connection()
    two = await connection('other')
    old = await member('existing')
    profile = {'tenant_key': 'tenant', 'open_id': 'subject', 'name': 'Alice', 'email': old.email}
    user, auth = await sso.bind_identity(one, profile)
    assert user.id != old.id and user.email.endswith('@sso.identity.invalid')
    assert (await sso.bind_identity(one, profile))[0].id == user.id
    assert (await sso.bind_identity(two, profile))[0].id != user.id
    no_email, _ = await sso.bind_identity(one, {'tenant_key': 'tenant', 'open_id': 'without-email'})
    assert no_email.id != user.id
    with pytest.raises(HTTPException) as error:
        await sso.bind_identity(one, {**profile, 'tenant_key': 'wrong'})
    assert error.value.status_code == 403
    async with get_async_db() as db:
        (await db.get(Auth, user.id)).active = False
        await db.commit()
    with pytest.raises(HTTPException):
        await sso.bind_identity(one, profile)


@pytest.mark.asyncio
async def test_sso_nonce_expiry_replay_and_revision(monkeypatch):
    await connection()
    user = await member('signed-in')
    monkeypatch.setattr(sso, 'exchange_identity', AsyncMock(return_value={}))
    monkeypatch.setattr(sso, 'bind_identity', AsyncMock(return_value=(user, SimpleNamespace())))
    monkeypatch.setattr(sso, 'create_signin_response', AsyncMock(return_value={'token': 'local-test-token'}))
    response = await sso.login('company')
    state = parse_qs(urlsplit(response.headers['location']).query)['state'][0]
    async with get_async_db() as db:
        row = await db.get(EnterpriseLoginState, state)
        cookie = 'enterprise_company=' + row.browser_nonce
    with pytest.raises(HTTPException):
        await sso.complete_callback('company', request('enterprise_company=wrong'), state, 'code')
    await sso.complete_callback('company', request(cookie), state, 'code')
    with pytest.raises(HTTPException):
        await sso.complete_callback('company', request(cookie), state, 'code')
    for change in ('expiry', 'revision', 'disabled'):
        response = await sso.login('company')
        state = parse_qs(urlsplit(response.headers['location']).query)['state'][0]
        async with get_async_db() as db:
            row = await db.get(EnterpriseLoginState, state)
            cookie = 'enterprise_company=' + row.browser_nonce
            if change == 'expiry':
                row.expires_at = int(time.time()) - 1
            if change == 'revision':
                row.revision -= 1
            if change == 'disabled':
                (await db.get(EnterpriseConnection, 'company')).enabled = False
            await db.commit()
        with pytest.raises(HTTPException):
            await sso.complete_callback('company', request(cookie), state, 'code')
    assert sso.exchange_identity.await_count == 1


@pytest.mark.asyncio
async def test_explicit_binding_and_stale_connection_cannot_create_user(monkeypatch):
    monkeypatch.setattr(sso, 'get_password_hash', AsyncMock(return_value='unused'))
    value = await connection()
    old = await member('legacy')
    admin = await member('admin', 'admin')
    from open_webui.socket import main as sockets

    monkeypatch.setattr(sockets, 'disconnect_user_sessions', AsyncMock())
    await sso.link_identity(
        'company',
        sso.IdentityLinkForm(
            tenant_id='tenant', subject='old-open-id', user_id=old.id, reason='Verified from enterprise directory'
        ),
        admin,
    )
    user, auth = await sso.bind_identity(value, {'tenant_key': 'tenant', 'open_id': 'old-open-id'})
    assert user.id == old.id and auth.session_stamp != 'original'
    async with get_async_db() as db:
        (await db.get(EnterpriseConnection, 'company')).revision += 1
        await db.commit()
    with pytest.raises(HTTPException):
        await sso.bind_identity(value, {'tenant_key': 'tenant', 'open_id': 'new-open-id'})


@pytest.mark.asyncio
async def test_project_private_chat_search_scope_and_revocation(monkeypatch):
    monkeypatch.setattr(projects, 'publish_event', AsyncMock())
    owner, alice, bob = [await member(name) for name in ('owner', 'alice', 'bob')]
    project = await projects.create_project(projects.ProjectForm(name='Project A'), request(), owner)
    await AccessGrants.set_access_grants(
        'folder',
        project.id,
        [
            {'principal_type': 'user', 'principal_id': alice.id, 'permission': 'read'},
        ],
    )
    async with get_async_db() as db:
        for identifier, user, visibility, folder in [
            ('private', alice, 'private', project.id),
            ('published', alice, 'project', project.id),
            ('elsewhere', alice, 'private', None),
        ]:
            db.add(
                Chat(
                    id=identifier,
                    user_id=user.id,
                    title='secret needle',
                    chat={'messages': []},
                    meta={},
                    folder_id=folder,
                    visibility=visibility,
                    archived=False,
                    created_at=0,
                    updated_at=0,
                )
            )
        db.add(
            Folder(id='legacy', user_id=owner.id, name='Old shared folder', kind='folder', created_at=0, updated_at=0)
        )
        db.add(
            Chat(
                id='legacy-chat',
                user_id=alice.id,
                title='Old semantics',
                chat={},
                meta={},
                folder_id='legacy',
                visibility='private',
                archived=False,
                created_at=0,
                updated_at=0,
            )
        )
        await db.commit()
    assert not await Chats.has_chat_access('private', owner)
    assert await Chats.has_chat_access('published', owner)
    assert not await Chats.has_chat_access('published', owner, permission='write')
    assert await Chats.has_chat_access('legacy-chat', owner)
    assert not await Chats.has_chat_access('published', bob)
    assert {r['id'] for r in await projects.project_chats(project.id, owner)} == {'published'}
    found = await Chats.get_chats_by_user_id_and_search_text(owner.id, 'needle', project_id=project.id)
    assert {row.id for row in found} == {'published'}
    assert await workspace_scope(alice, {'chat_id': 'private'}) == project.id
    with pytest.raises(HTTPException):
        await workspace_scope(owner, {'chat_id': 'published'})
    with pytest.raises(HTTPException):
        await projects.visibility(
            project.id, 'private', projects.VisibilityForm(visibility='project'), request(), owner
        )
    await AccessGrants.set_access_grants('folder', project.id, [])
    assert not await Chats.has_chat_access('published', bob)
    with pytest.raises(HTTPException):
        await workspace_scope(alice, {'project_id': project.id})


@pytest.mark.asyncio
async def test_personal_mcp_secrets_revision_acl_and_platform_key_disable():
    owner, other = await member('owner'), await member('other')
    await Config.upsert(
        {'team.enabled': True, 'user.permissions': {'features': {'direct_tool_servers': True, 'api_keys': True}}}
    )
    identifier = uuid.uuid4().hex
    saved = await connections.save_connection(
        identifier,
        connections.ConnectionForm(
            name='Mine', url='https://mcp.example.com/mcp', auth_type='bearer', key='private-bearer'
        ),
        request(),
        owner,
    )
    assert saved['has_key'] and 'private-bearer' not in str(saved)
    config = await personal_mcp_connection(saved['server_id'], owner.id)
    assert config['key'] == 'private-bearer'
    with pytest.raises(HTTPException):
        await personal_mcp_connection(saved['server_id'], other.id)
    with pytest.raises(HTTPException):
        await connections.save_connection(
            identifier, connections.ConnectionForm(name='Hijack', url='https://mcp.example.com'), request(), other
        )
    changed = await connections.save_connection(
        identifier, connections.ConnectionForm(name='Changed', url='https://mcp.example.com/mcp'), request(), owner
    )
    with pytest.raises(HTTPException):
        await personal_mcp_connection(saved['server_id'], owner.id)
    assert changed['server_id'] != saved['server_id']
    assert not await has_permission(owner.id, 'features.api_keys', {'features': {'api_keys': True}})


@pytest.mark.asyncio
async def test_dns_pinning_and_explicit_local_origin(monkeypatch):
    from open_webui.utils.team_network import private_host_allowed

    monkeypatch.setenv('PERSONAL_MCP_ALLOWED_PRIVATE_ORIGINS', 'https://mcp.corp.example')
    assert private_host_allowed('mcp.corp.example', 443)
    assert not private_host_allowed('mcp.corp.example', 80)
    monkeypatch.delenv('PERSONAL_MCP_ALLOWED_PRIVATE_ORIGINS')
    with pytest.raises(ValueError):
        validate_endpoint('http://127.0.0.1/mcp')
    with pytest.raises(ValueError):
        await resolved_address('127.0.0.1', 80)

    async def handler(request):
        return web.json_response({'host': request.host})

    app = web.Application()
    app.router.add_get('/mcp', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    monkeypatch.setenv('PERSONAL_MCP_ALLOWED_PRIVATE_ORIGINS', origin)
    try:
        import httpx, httpx2

        for version, client in ((1, httpx.AsyncClient), (2, httpx2.AsyncClient)):
            async with client(transport=member_http_transport(version=version)) as session:
                result = await session.get(origin + '/mcp')
                assert result.json()['host'] == f'127.0.0.1:{port}'
        async with oauth_http_session(restricted=True) as session:
            async with session.get(origin + '/mcp') as response:
                assert (await response.json())['host'] == f'127.0.0.1:{port}'
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_personal_mcp_real_handshake_tools_and_owner_credentials(monkeypatch):
    from open_webui.utils.tools import connect_mcp_server

    owner, other = await member('mcp-owner'), await member('mcp-other')
    await Config.upsert({'user.permissions': {'features': {'direct_tool_servers': True}}})
    received = []

    async def mcp_handler(req):
        if req.method != 'POST':
            return web.Response(status=405)
        data = await req.json()
        received.append((data['method'], req.headers.get('Authorization'), req.headers.get('Cookie')))
        assert req.headers.get('Authorization') == 'Bearer consumer-only-secret'
        if 'id' not in data:
            return web.Response(status=202)
        if data['method'] == 'initialize':
            result = {
                'protocolVersion': '2025-06-18',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'Local acceptance MCP', 'version': '1'},
            }
        elif data['method'] == 'tools/list':
            result = {
                'tools': [
                    {
                        'name': 'echo',
                        'description': 'Local owner-only echo',
                        'inputSchema': {
                            'type': 'object',
                            'properties': {'message': {'type': 'string'}},
                            'required': ['message'],
                        },
                    }
                ]
            }
        elif data['method'] == 'tools/call':
            result = {'content': [{'type': 'text', 'text': data['params']['arguments']['message']}], 'isError': False}
        else:
            return web.json_response(
                {'jsonrpc': '2.0', 'id': data['id'], 'error': {'code': -32601, 'message': 'Method not found'}}
            )
        return web.json_response({'jsonrpc': '2.0', 'id': data['id'], 'result': result})

    app = web.Application()
    app.router.add_route('*', '/mcp', mcp_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    origin = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    monkeypatch.setenv('PERSONAL_MCP_ALLOWED_PRIVATE_ORIGINS', origin)
    client = None
    try:
        saved = await connections.save_connection(
            uuid.uuid4().hex,
            connections.ConnectionForm(
                name='Local MCP', url=origin + '/mcp', auth_type='bearer', key='consumer-only-secret'
            ),
            request('platform-session=not-for-mcp'),
            owner,
        )
        client, specs = await connect_mcp_server(request(), saved['server_id'], owner, {})
        assert specs[0]['name'] == 'echo'
        result = await client.call_tool('echo', {'message': 'owner workspace'})
        assert any(item.get('text') == 'owner workspace' for item in result)
        count = len(received)
        with pytest.raises(HTTPException) as error:
            await connect_mcp_server(request(), saved['server_id'], other, {})
        assert error.value.status_code == 404 and len(received) == count
        assert all(key == 'Bearer consumer-only-secret' and cookie is None for _, key, cookie in received)
    finally:
        if client:
            await client.disconnect()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_sandbox_descriptor_cache_never_imports_source(monkeypatch):
    from open_webui.utils import plugin
    from open_webui.models.tools import ToolForm, ToolMeta, Tools

    owner = await member('author')
    await Config.upsert({'team.enabled': True})
    tool = await Tools.insert_new_tool(
        owner.id,
        ToolForm(
            id='personal_tool',
            name='Personal',
            content="raise RuntimeError('MUST NOT RUN ON HOST')",
            meta=ToolMeta(runtime='sandbox', sandbox_schema={'specs': [], 'valves_schema': None}),
        ),
        [],
    )
    req = request()
    descriptor, _ = await plugin.get_tool_module_from_cache(req, tool.id)
    assert isinstance(descriptor, SandboxTool)
    assert (await plugin.get_tool_module_from_cache(req, tool.id, load_from_db=False))[0] is descriptor


@pytest.mark.asyncio
async def test_shared_tool_runs_as_consumer_without_author_credentials(monkeypatch):
    from open_webui.models.tools import ToolForm, ToolMeta, Tools
    from open_webui.utils import sandboxes

    author, consumer = await member('author'), await member('consumer')
    await Config.upsert({'team.enabled': True})
    tool = await Tools.insert_new_tool(
        author.id,
        ToolForm(
            id='shared',
            name='Shared',
            content='class Tools: pass',
            meta=ToolMeta(runtime='sandbox'),
            access_grants=[{'principal_type': 'user', 'principal_id': consumer.id, 'permission': 'read'}],
        ),
        [{'name': 'run', 'parameters': {'type': 'object', 'properties': {}}}],
    )
    owner_valves = AsyncMock(return_value={'key': 'author-secret'})
    monkeypatch.setattr(Tools, 'get_tool_valves_by_id', owner_valves)
    monkeypatch.setattr(Tools, 'get_user_valves_by_id_and_user_id', AsyncMock(return_value={'key': 'consumer-secret'}))
    execute = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(sandboxes, 'sandbox_python', execute)
    metadata = {'chat_id': 'local:consumer'}
    functions = await sandboxes.sandbox_tool_functions(tool, consumer, metadata)
    await functions['run']['callable'](value=3)
    args = execute.await_args.args
    assert args[0].id == consumer.id and args[1] == metadata
    assert args[2]['valves'] == {} and args[2]['user_valves'] == {'key': 'consumer-secret'}
    assert 'author-secret' not in str(args)
    owner_valves.assert_not_awaited()
    await AccessGrants.set_access_grants('tool', tool.id, [])
    with pytest.raises(HTTPException) as error:
        await functions['run']['callable'](value=4)
    assert error.value.status_code == 403 and execute.await_count == 1


@pytest.mark.asyncio
async def test_python_helper_stages_source_as_data_and_keeps_project_scope(monkeypatch):
    from open_webui.utils import sandboxes

    user = await member('coder')
    execute = AsyncMock(return_value={'stdout': 'done', 'stderr': '', 'result': ''})
    monkeypatch.setattr(sandboxes, 'sandbox_python', execute)
    source = 'print("quotes\' and shell $(not-a-command)")'
    metadata = {'project_id': 'project-id'}
    result = await sandboxes.sandbox_execute_code(user, metadata, source)
    args = execute.await_args.args
    assert args[0].id == user.id and args[1] is metadata and result['stdout'] == 'done'
    assert args[2]['params']['code'] == source and source not in args[2]['content']
    assert args[2]['action'] == 'execute'


@pytest.mark.asyncio
async def test_file_import_requires_current_access_and_explicit_publication(monkeypatch):
    import os
    from pathlib import Path

    import aiohttp
    from open_webui.models.files import File
    from open_webui.utils import terminals, tether
    from open_webui.utils.access_control.files import has_access_to_file

    monkeypatch.setattr(projects, 'publish_event', AsyncMock())
    author, reader = await member('author'), await member('reader')
    project = await projects.create_project(projects.ProjectForm(name='References'), request(), author)
    await AccessGrants.set_access_grants(
        'folder', project.id, [{'principal_type': 'user', 'principal_id': reader.id, 'permission': 'read'}]
    )
    path = Path(os.environ['DATA_DIR']) / 'input.csv'
    path.write_bytes(b'value\n42\n')
    async with get_async_db() as db:
        db.add(
            File(
                id='input',
                user_id=author.id,
                filename='input.csv',
                path=str(path),
                data={},
                meta={},
                created_at=0,
                updated_at=0,
            )
        )
        await db.commit()
    assert not await has_access_to_file('input', 'read', reader)
    await projects.publish_file(project.id, projects.PublishFileForm(file_id='input'), author)
    assert await has_access_to_file('input', 'read', reader)
    assert not await has_access_to_file('input', 'write', reader)
    uploaded = []

    async def receive(req):
        part = await (await req.multipart()).next()
        uploaded.append(await part.read())
        return web.json_response({'path': '/home/user/input.csv'})

    app = web.Application()
    app.router.add_post('/files/upload', receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(
        terminals, 'resolve_terminal_connection', AsyncMock(return_value=({'url': f'http://127.0.0.1:{port}'}, {}, {}))
    )
    monkeypatch.setattr(tether, 'get_terminal_client', lambda *args, **kwargs: aiohttp.ClientSession())
    try:
        result = await terminals.get_terminal_json(
            request(), reader, {'terminal_id': 'managed'}, '/files/upload', file={'id': 'input', 'name': 'input.csv'}
        )
        assert result['path'] == '/home/user/input.csv' and uploaded == [b'value\n42\n']
        await projects.unpublish_file(project.id, 'input', author)
        assert not await has_access_to_file('input', 'read', reader)
        assert (
            await terminals.get_terminal_json(
                request(),
                reader,
                {'terminal_id': 'managed'},
                '/files/upload',
                file={'id': 'input', 'name': 'input.csv'},
            )
            is None
        )
        assert len(uploaded) == 1
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_personal_model_preset_cannot_bypass_base_model_acl(monkeypatch):
    from open_webui.models.models import Models
    from open_webui.utils.access_control import check_model_access

    user = await member('preset-owner')
    base = SimpleNamespace(id='admin-model', user_id='admin', base_model_id=None)
    preset = SimpleNamespace(id='my-preset', user_id=user.id, base_model_id=base.id)
    monkeypatch.setattr(Models, 'get_model_by_id', AsyncMock(return_value=base))
    with pytest.raises(HTTPException) as error:
        await check_model_access(user, preset)
    assert error.value.status_code == 403
    await AccessGrants.set_access_grants(
        'model', base.id, [{'principal_type': 'user', 'principal_id': user.id, 'permission': 'read'}]
    )
    await check_model_access(user, preset)
    await AccessGrants.set_access_grants('model', base.id, [])
    with pytest.raises(HTTPException):
        await check_model_access(user, preset)


@pytest.mark.asyncio
async def test_existing_member_api_key_is_rejected_by_authentication_gate():
    from open_webui.models.users import Users
    from open_webui.utils.auth import get_current_user_by_api_key

    user, admin = await member('member'), await member('admin', 'admin')
    await Users.update_user_api_key_by_id(user.id, 'sk-existing-member')
    await Users.update_user_api_key_by_id(admin.id, 'sk-admin')
    await Config.upsert({'auth.enable_api_keys': True, 'user.permissions': {'features': {'api_keys': True}}})
    assert (await get_current_user_by_api_key(request(), 'sk-existing-member')).id == user.id
    await Config.upsert({'team.enabled': True})
    with pytest.raises(HTTPException) as error:
        await get_current_user_by_api_key(request(), 'sk-existing-member')
    assert error.value.status_code == 403
    assert (await get_current_user_by_api_key(request(), 'sk-admin')).id == admin.id


@pytest.mark.asyncio
async def test_sso_new_account_follows_default_role_and_is_audited(monkeypatch):
    from open_webui.models.team import QuotaAudit
    from sqlalchemy import select

    monkeypatch.setattr(sso, 'get_password_hash', AsyncMock(return_value='unused'))
    value = await connection()
    await Config.upsert({'ui.default_user_role': 'pending'})
    pending, _ = await sso.bind_identity(value, {'tenant_key': 'tenant', 'open_id': 'needs-approval'})
    assert pending.role == 'pending'
    # A pending account can still sign in to reach the awaiting-approval screen.
    assert (await sso.bind_identity(value, {'tenant_key': 'tenant', 'open_id': 'needs-approval'}))[0].id == pending.id
    await Config.upsert({'ui.default_user_role': 'admin'})  # never escalates through SSO
    capped, _ = await sso.bind_identity(value, {'tenant_key': 'tenant', 'open_id': 'no-admin'})
    assert capped.role == 'pending'
    async with get_async_db() as db:
        actions = (
            await db.execute(select(QuotaAudit.action).where(QuotaAudit.user_id.in_([pending.id, capped.id])))
        ).scalars()
        assert sorted(actions) == ['enterprise_user_created', 'enterprise_user_created']


@pytest.mark.asyncio
async def test_project_save_detects_stale_editor_and_keeps_published_files(monkeypatch):
    from open_webui.models.team import QuotaAudit
    from sqlalchemy import select

    monkeypatch.setattr(projects, 'publish_event', AsyncMock())
    owner, editor = await member('owner'), await member('editor')
    project = await projects.create_project(projects.ProjectForm(name='P'), request(), owner)
    await AccessGrants.set_access_grants(
        'folder',
        project.id,
        [
            {'principal_type': 'user', 'principal_id': editor.id, 'permission': 'read'},
            {'principal_type': 'user', 'principal_id': editor.id, 'permission': 'write'},
        ],
    )
    form = lambda **kw: projects.ProjectSaveForm(**{'name': 'P', 'system_prompt': '', 'model_ids': [], **kw})
    saved = await projects.save_project(project.id, form(system_prompt='owner', revision=0), request(), owner)
    assert saved['data']['context_revision'] == 1
    # A file published meanwhile must survive any later save.
    async with get_async_db() as db:
        row = await db.get(Folder, project.id)
        row.data = {**row.data, 'files': [{'id': 'f', 'type': 'file', 'name': 'f'}]}
        await db.commit()
    with pytest.raises(HTTPException) as error:
        await projects.save_project(project.id, form(system_prompt='editor', revision=0), request(), editor)
    assert error.value.status_code == 409 and error.value.detail['updated_by'] == owner.id
    forced = await projects.save_project(
        project.id, form(system_prompt='editor', revision=0, force=True), request(), editor
    )
    assert forced['data']['system_prompt'] == 'editor' and forced['data']['files'][0]['id'] == 'f'
    async with get_async_db() as db:
        logs = (
            (
                await db.execute(
                    select(QuotaAudit)
                    .where(QuotaAudit.action == 'project_context_update')
                    .order_by(QuotaAudit.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [log.data['overwrote_newer'] for log in logs] == [False, True]
    assert logs[-1].data['changes']['system_prompt'] == {'from': 'owner', 'to': 'editor'}


@pytest.mark.asyncio
async def test_team_mode_publishes_new_connection_models_once(monkeypatch):
    from open_webui.models.models import Models
    from open_webui.utils import models as model_utils
    from open_webui.utils.access_control import check_model_access

    admin = await member('first-admin', role='admin')
    user = await member('plain-member')
    found = [
        {'id': 'gpt-x', 'name': 'GPT X', 'owned_by': 'openai'},
        {'id': 'my-pipe', 'name': 'Pipe', 'owned_by': 'openai', 'pipe': {'type': 'pipe'}},
        {'id': 'arena-model', 'name': 'Arena', 'owned_by': 'arena', 'arena': True},
        {'id': 'bad id', 'name': 'Bad', 'owned_by': 'openai'},
    ]

    # Upstream behaviour outside team mode: nothing is written.
    assert not await model_utils.publish_new_base_models(found, await Models.get_all_models())
    await Config.upsert({'team.enabled': True})
    assert await model_utils.publish_new_base_models(found, await Models.get_all_models())

    rows = {row.id: row for row in await Models.get_all_models()}
    assert set(rows) == {'gpt-x'}
    assert rows['gpt-x'].user_id == admin.id and rows['gpt-x'].name == 'GPT X'
    assert rows['gpt-x'].meta.model_dump().get('defaultFeatureIds') == ['web_search']
    await check_model_access(user, rows['gpt-x'])

    # The admin's later choice (private here) is kept; the row is never rewritten.
    await AccessGrants.set_access_grants('model', 'gpt-x', [])
    assert not await model_utils.publish_new_base_models(found, await Models.get_all_models())
    with pytest.raises(HTTPException):
        await check_model_access(user, await Models.get_model_by_id('gpt-x'))

    monkeypatch.setattr(model_utils, 'TEAM_PUBLIC_BASE_MODELS', False)
    assert not await model_utils.publish_new_base_models(
        [{'id': 'gpt-y', 'owned_by': 'openai'}], await Models.get_all_models()
    )

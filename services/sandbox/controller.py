"""Single-worker Docker lifecycle service. No enterprise Terminals code is used.

Only this service has the Docker socket. Runtime traffic is credential-stripped,
scoped by the trusted WebUI server and protected by per-network firewall rules.
"""

import asyncio
import contextlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import docker
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.requests import HTTPConnection

KEY = os.environ.get('SANDBOX_CONTROLLER_KEY', '')
IMAGE = os.environ.get('SANDBOX_RUNTIME_IMAGE', 'open-webui-sandbox-runtime:0.17.7')
HELPER_IMAGE = os.environ.get('SANDBOX_CONTROLLER_IMAGE', 'open-webui-sandbox-controller:1')
CONTROLLER = os.environ.get('SANDBOX_CONTROLLER_CONTAINER', 'owui-sandbox')
PROXY = os.environ.get('SANDBOX_PROXY_CONTAINER', 'owui-egress')
NAMESPACE = os.environ.get('SANDBOX_NAMESPACE', 'owui')
if not re.fullmatch(r'[a-z0-9-]{1,32}', NAMESPACE):
    raise RuntimeError('SANDBOX_NAMESPACE must use 1–32 lowercase letters, digits or hyphens')
LABEL = {'app': 'open-webui-managed-sandbox', 'owner': NAMESPACE}
# Sized for an 8-core / 32 GB host: 10 x 2 GiB caps fit beside WebUI, embedding and the database
# without memory overcommit. Lower it on smaller hosts (about 2 GiB of free RAM per sandbox).
MAX_RUNNING = int(os.environ.get('SANDBOX_MAX_RUNNING', '10'))
IDLE = int(os.environ.get('SANDBOX_IDLE_SECONDS', '600'))
# open-terminal's built-in Chromium (browser tools for the model + Browser tab in the UI).
# Off by default: workspaces are offline unless a member enables public access.
BROWSER = os.environ.get('SANDBOX_ENABLE_BROWSER', 'false').strip().lower() == 'true'
BROWSER_ENV = 'OPEN_TERMINAL_ENABLE_BROWSER=' + ('true' if BROWSER else 'false')
TIMEOUT = int(os.environ.get('SANDBOX_EXECUTION_SECONDS', '120'))
MONITOR_SECONDS = float(os.environ.get('SANDBOX_MONITOR_SECONDS', '20'))
state_dir = Path(os.environ.get('SANDBOX_STATE_DIR', '/state'))
state_dir.mkdir(parents=True, exist_ok=True)
state = sqlite3.connect(state_dir / 'workspaces.db')
state.row_factory = sqlite3.Row
state.execute("""CREATE TABLE IF NOT EXISTS workspace (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, project_id TEXT NOT NULL, api_key TEXT NOT NULL,
    public INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'stopped', error TEXT,
    UNIQUE(user_id, project_id))""")
state.commit()
locks = {}
lifecycle_locks = {}
resumed = {}
agent_waiters = {}
agent_running = set()
manual = set()
active = set()
last_used = {}
queue = []
starters = {}
capacity = asyncio.Condition()
docker_lock = asyncio.Lock()


def lifecycle(row):
    return lifecycle_locks.setdefault(row['id'], asyncio.Lock())


def resume_event(identifier):
    if identifier not in resumed:
        resumed[identifier] = asyncio.Event()
        resumed[identifier].set()
    return resumed[identifier]


async def docker_call(function, *args):
    # A cancelled HTTP request cannot leave a Docker thread running after its
    # lifecycle lock has been released.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await task
        raise


@asynccontextmanager
async def agent_execution(row):
    identifier = row['id']
    lock = locks.setdefault(identifier, asyncio.Lock())
    agent_waiters[identifier] = agent_waiters.get(identifier, 0) + 1
    acquired = False
    try:
        while True:
            await resume_event(identifier).wait()
            await lock.acquire()
            if identifier not in manual:
                acquired = True
                break
            lock.release()
        agent_running.add(identifier)
        yield
    finally:
        agent_waiters[identifier] -= 1
        if acquired:
            agent_running.discard(identifier)
            last_used[identifier] = time.monotonic()
            lock.release()


async def take_manual_control(row):
    manual.add(row['id'])
    resume_event(row['id']).clear()
    async with locks.setdefault(row['id'], asyncio.Lock()):
        pass  # Confirm takeover only after the current Agent attempt finishes.


@contextlib.contextmanager
def client():
    api = docker.from_env(timeout=30)
    try:
        yield api
    finally:
        api.close()


def ensure_image(api, image):
    """The Docker API never pulls on create; fetch a missing prebuilt image (e.g. from GHCR)."""
    try:
        api.images.get(image)
    except docker.errors.ImageNotFound:
        repository, _, tag = image.rpartition(':') if ':' in image.rsplit('/', 1)[-1] else (image, '', 'latest')
        api.images.pull(repository, tag=tag)


def get_workspace(user_id, project_id):
    if len(user_id) > 100 or len(project_id) > 100 or not user_id or not project_id:
        raise HTTPException(400, 'Invalid workspace scope')
    row = state.execute('SELECT * FROM workspace WHERE user_id=? AND project_id=?', (user_id, project_id)).fetchone()
    if row is None:
        state.execute(
            'INSERT INTO workspace (id,user_id,project_id,api_key) VALUES (?,?,?,?)',
            (str(uuid.uuid4()), user_id, project_id, secrets.token_urlsafe(32)),
        )
        state.commit()
        row = state.execute(
            'SELECT * FROM workspace WHERE user_id=? AND project_id=?', (user_id, project_id)
        ).fetchone()
    return dict(row)


def set_status(row, status, error=None):
    state.execute('UPDATE workspace SET status=?, error=? WHERE id=?', (status, error, row['id']))
    state.commit()
    row.update(status=status, error=error)


def name(row):
    return NAMESPACE + '-ws-' + row['id']


def container_for(api, row):
    try:
        container = api.containers.get(name(row))
    except docker.errors.NotFound:
        return None
    if any(container.labels.get(k) != v for k, v in LABEL.items()):
        raise RuntimeError('Refusing to manage an unlabelled container')
    return container


def configure_firewall(api, row, runtime, network):
    controller = api.containers.get(CONTROLLER)
    controller.reload()
    if network.name not in controller.attrs['NetworkSettings']['Networks']:
        network.connect(controller)
        controller.reload()
    controller_ip = controller.attrs['NetworkSettings']['Networks'][network.name]['IPAddress']
    proxy_ip = ''
    if row['public']:
        proxy = api.containers.get(PROXY)
        proxy.reload()
        if network.name not in proxy.attrs['NetworkSettings']['Networks']:
            network.connect(proxy)
            proxy.reload()
        proxy_ip = proxy.attrs['NetworkSettings']['Networks'][network.name]['IPAddress']
    api.containers.run(
        HELPER_IMAGE,
        command=['/app/firewall.sh', controller_ip, proxy_ip],
        network_mode='container:' + runtime.id,
        cap_drop=['ALL'],
        cap_add=['NET_ADMIN'],
        security_opt=['no-new-privileges:true'],
        read_only=True,
        remove=True,
        user='0:0',
    )
    # /run is root-owned. Untrusted code cannot start before the firewall succeeds.
    result = runtime.exec_run(['touch', '/run/network-ready'], user='0:0')
    if result.exit_code:
        raise RuntimeError('Unable to activate network boundary')
    runtime.reload()
    return runtime.attrs['NetworkSettings']['Networks'][network.name]['IPAddress']


def runtime_matches_settings(runtime):
    return BROWSER_ENV in ((runtime.attrs.get('Config') or {}).get('Env') or [])


def start_runtime(row):
    with client() as api:
        network_name = name(row)
        try:
            network = api.networks.get(network_name)
            if any(network.attrs.get('Labels', {}).get(k) != v for k, v in LABEL.items()):
                raise RuntimeError('Refusing to reuse an unlabelled network')
        except docker.errors.NotFound:
            network = api.networks.create(network_name, internal=True, labels=LABEL)
        proxy_url = 'http://127.0.0.1:3128'
        if row['public']:
            egress = api.containers.get(PROXY)
            egress.reload()
            if network_name not in egress.attrs['NetworkSettings']['Networks']:
                network.connect(egress)
                egress.reload()
            proxy_url = 'http://' + egress.attrs['NetworkSettings']['Networks'][network_name]['IPAddress'] + ':3128'
        runtime = container_for(api, row)
        if runtime is not None and runtime.status != 'running' and not runtime_matches_settings(runtime):
            # Settings are baked into the container at creation; apply changes on the next start.
            runtime.remove()  # The home volume is kept.
            runtime = None
        if runtime is None:
            ensure_image(api, IMAGE)
            ensure_image(api, HELPER_IMAGE)
            volume = api.volumes.create(name=name(row) + '-home', labels=LABEL)
            # Named volumes inherit the image's non-root home ownership on first mount.
            runtime = api.containers.create(
                IMAGE,
                name=name(row),
                labels=LABEL,
                user='1000:1000',
                volumes={volume.name: {'bind': '/home/user', 'mode': 'rw'}},
                network=network_name,
                nano_cpus=1_000_000_000,
                mem_limit='2g',
                memswap_limit='2g',
                pids_limit=128,
                cap_drop=['ALL'],
                security_opt=['no-new-privileges:true'],
                read_only=True,
                tmpfs={
                    '/tmp': 'rw,nosuid,nodev,size=512m',
                    '/run': 'rw,nosuid,nodev,size=64m',
                    '/var/tmp': 'rw,nosuid,nodev,size=128m',
                },
                shm_size='256m',
                environment={
                    'OPEN_TERMINAL_API_KEY': row['api_key'],
                    'OPEN_TERMINAL_ENABLE_BROWSER': 'true' if BROWSER else 'false',
                    'HTTP_PROXY': proxy_url,
                    'HTTPS_PROXY': proxy_url,
                    'http_proxy': proxy_url,
                    'https_proxy': proxy_url,
                },
                init=False,
            )
        # On restart /run is fresh and no user process runs before firewall activation.
        if runtime.status != 'running':
            runtime.start()
        try:
            ip = configure_firewall(api, row, runtime, network)
            return ip
        except BaseException:
            runtime.stop(timeout=2)
            raise


def stop_runtime(row, recreate=False):
    with client() as api:
        runtime = container_for(api, row)
        if runtime:
            runtime.stop(timeout=5)
            if recreate:
                runtime.remove()  # Never remove the persistent home volume here.


async def ensure(row):
    # Exactly one waiter per workspace may enter the global capacity queue.
    async with lifecycle(row):
        return await ensure_locked(row)


async def ensure_locked(row):
    identifier = row['id']
    last_used[identifier] = time.monotonic()
    async with capacity:
        if identifier not in active:
            if identifier not in queue:
                queue.append(identifier)
            set_status(row, 'queued')
            try:
                await capacity.wait_for(lambda: len(active) < MAX_RUNNING and queue and queue[0] == identifier)
                queue.pop(0)
                active.add(identifier)
                capacity.notify_all()
            except BaseException:
                if identifier in queue:
                    queue.remove(identifier)
                    capacity.notify_all()
                raise
    if not row.get('ip'):
        set_status(row, 'starting')
        try:
            async with docker_lock:
                row['ip'] = await docker_call(start_runtime, row)
            async with aiohttp.ClientSession() as session:
                for attempt in range(60):
                    try:
                        async with session.get(
                            f'http://{row["ip"]}:8000/openapi.json',
                            headers={'Authorization': 'Bearer ' + row['api_key']},
                            timeout=aiohttp.ClientTimeout(total=2),
                        ) as response:
                            if response.status == 200:
                                break
                    except (aiohttp.ClientError, TimeoutError):
                        pass
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError('Runtime did not become healthy')
            set_status(row, 'running')
        except BaseException as error:
            async with docker_lock:
                with contextlib.suppress(Exception):
                    await docker_call(stop_runtime, row)
            row.pop('ip', None)
            set_status(row, 'failed', str(error)[:1000])
            async with capacity:
                active.discard(identifier)
                capacity.notify_all()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise HTTPException(503, 'Workspace failed to start; files are retained') from error
    return row['ip']


# Cache holds no caller tokens or platform credentials.
workspaces = {}


def workspace(user, project):
    key = (user, project)
    if key not in workspaces:
        workspaces[key] = get_workspace(user, project)
    return workspaces[key]


async def authenticate(request: HTTPConnection):
    if request.scope['type'] == 'websocket':
        return  # First-message authentication is performed before any runtime is resolved.
    if not KEY or not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + KEY):
        raise HTTPException(401, 'Invalid controller credential')


async def idle_monitor():
    while True:
        await asyncio.sleep(MONITOR_SECONDS)
        for row in list(workspaces.values()):
            identifier = row['id']
            lock = locks.setdefault(identifier, asyncio.Lock())
            if identifier in active and not lock.locked() and time.monotonic() - last_used.get(identifier, 0) > IDLE:
                async with lock, lifecycle(row), docker_lock:
                    # Recheck after acquiring locks: another request may have used it.
                    if time.monotonic() - last_used.get(identifier, 0) <= IDLE:
                        continue
                    await docker_call(stop_runtime, row)
                    row.pop('ip', None)
                    set_status(row, 'stopped')
                    async with capacity:
                        active.discard(identifier)
                        capacity.notify_all()


@asynccontextmanager
async def lifespan(app):
    if not KEY:
        raise RuntimeError('SANDBOX_CONTROLLER_KEY is required')
    # Stop only this controller's labelled runtimes; preserve all volumes on recovery.
    with client() as api:
        for container in api.containers.list(filters={'label': [f'{key}={value}' for key, value in LABEL.items()]}):
            await asyncio.to_thread(container.stop, timeout=5)
    state.execute("UPDATE workspace SET status='stopped', error=NULL")
    state.commit()
    task = asyncio.create_task(idle_monitor())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan, dependencies=[Depends(authenticate)])


@app.get('/health')
async def health():
    return {'running': len(active), 'queued': len(queue), 'limit': MAX_RUNNING}


@app.get('/workspaces/{user}/{project}')
async def status(user: str, project: str):
    row = workspace(user, project)
    return {key: row.get(key) for key in ('id', 'status', 'public', 'error')} | {
        'manual': row['id'] in manual,
        'agent_status': 'running'
        if row['id'] in agent_running
        else 'paused'
        if row['id'] in manual
        else 'queued'
        if agent_waiters.get(row['id'], 0)
        else 'idle',
        'agent_queue': max(0, agent_waiters.get(row['id'], 0) - int(row['id'] in agent_running)),
        'queue_position': queue.index(row['id']) + 1 if row['id'] in queue else None,
    }


class Control(BaseModel):
    action: str
    confirm: str | None = None


@app.post('/workspaces/{user}/{project}')
async def control(user: str, project: str, form: Control):
    row = workspace(user, project)
    identifier = row['id']
    lock = locks.setdefault(identifier, asyncio.Lock())
    if form.action == 'start':
        # Return queue state immediately, not a browser request held open for the
        # entire global capacity wait. Runtime requests still await the same lock.
        if identifier not in starters or starters[identifier].done():

            async def start():
                with contextlib.suppress(HTTPException):
                    await ensure(row)

            starters[identifier] = asyncio.create_task(start())
            await asyncio.sleep(0)
    elif form.action in {'pause', 'resume'}:
        if form.action == 'pause':
            await take_manual_control(row)
        else:
            manual.discard(identifier)
            resume_event(identifier).set()
    elif form.action in {'stop', 'recreate', 'offline', 'public', 'clear'}:
        if form.action == 'clear' and form.confirm != 'CLEAR MY WORKSPACE':
            raise HTTPException(400, 'Explicit clear confirmation required')
        if task := starters.get(identifier):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        async with lock, lifecycle(row), docker_lock:
            await docker_call(stop_runtime, row, form.action != 'stop')
            if form.action == 'clear':
                with client() as api:
                    try:
                        volume = api.volumes.get(name(row) + '-home')
                    except docker.errors.NotFound:
                        volume = None
                    if volume:
                        if any(volume.attrs.get('Labels', {}).get(k) != v for k, v in LABEL.items()):
                            raise HTTPException(409, 'Volume ownership mismatch')
                        await docker_call(volume.remove)
            if form.action in {'public', 'offline'}:
                row['public'] = int(form.action == 'public')
                state.execute('UPDATE workspace SET public=? WHERE id=?', (row['public'], identifier))
            row.pop('ip', None)
            set_status(row, 'stopped')
            async with capacity:
                active.discard(identifier)
                capacity.notify_all()
    else:
        raise HTTPException(400, 'Unknown workspace action')
    return await status(user, project)


HOP_HEADERS = {
    'connection',
    'transfer-encoding',
    'content-length',
    'content-encoding',
    'set-cookie',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'upgrade',
}


def runtime_headers(row, request):
    # Never forward controller credentials, WebUI tokens, cookies or model-provider headers.
    result = {'Authorization': 'Bearer ' + row['api_key'], 'X-User-Id': row['user_id']}
    for key in ('content-type', 'accept', 'range', 'x-session-id'):
        if value := request.headers.get(key):
            result[key] = value
    return result


async def abort_execution(row, session, base, headers, process_id=None, *, timeout=False):
    if process_id:
        with contextlib.suppress(Exception):
            async with session.delete(
                f'{base}/execute/{process_id}',
                params={'force': 'true'},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                await response.read()
    # Stopping the container also reaps detached children and covers cancellation
    # during the initial POST, before a process ID was returned. Files stay mounted.
    async with lifecycle(row), docker_lock:
        await docker_call(stop_runtime, row)
        row.pop('ip', None)
        set_status(row, 'timeout' if timeout else 'stopped')
        async with capacity:
            active.discard(row['id'])
            capacity.notify_all()


async def run_agent_command(row, session, base, headers, payload, query=None):
    process_id = None
    try:
        async with asyncio.timeout(TIMEOUT):
            async with session.post(base + '/execute', params={'wait': '0'}, json=payload, headers=headers) as response:
                result = await response.json()
                if response.status >= 400:
                    return result, response.status
            process_id = result.get('id')
            if not process_id:
                raise RuntimeError('Runtime returned no process ID')
            if result.get('status') == 'running':
                params = {'wait': str(TIMEOUT), **({k: v for k, v in (query or {}).items() if k == 'tail'})}
                async with session.get(
                    f'{base}/execute/{process_id}/status', params=params, headers=headers
                ) as response:
                    result = await response.json()
                    if response.status >= 400:
                        raise RuntimeError('Runtime could not confirm command completion')
            if result.get('status') == 'running':
                raise TimeoutError()
            set_status(row, 'running')
            return result, 200
    except BaseException as error:
        cleanup = asyncio.create_task(
            abort_execution(row, session, base, headers, process_id, timeout=isinstance(error, TimeoutError))
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
        if isinstance(error, TimeoutError):
            return {
                'id': process_id,
                'status': 'timeout',
                'error': f'Agent execution exceeded {TIMEOUT} seconds; files retained',
            }, 408
        raise


@app.api_route('/r/{user}/{project}/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
async def proxy(user: str, project: str, path: str, request: Request):
    if path.startswith(('api/agents', 'agents', 'admin', 'api/admin')):
        raise HTTPException(403, 'Unmetered model agents and runtime administration are disabled')
    row = workspace(user, project)
    identifier = row['id']
    agent = request.headers.get('x-agent-execution') == '1'
    if not agent and request.method == 'POST' and path in {'execute', 'api/terminals/user/input'}:
        await take_manual_control(row)
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT + 15))
    context = agent_execution(row) if agent else None
    entered = False
    try:
        if agent:
            await context.__aenter__()
            entered = True
        ip = await ensure(row)
        params = list(request.query_params.multi_items())
        if agent and path == 'execute' and request.method == 'POST':
            result, code = await run_agent_command(
                row,
                session,
                f'http://{ip}:8000',
                runtime_headers(row, request),
                await request.json(),
                dict(request.query_params),
            )
            await session.close()
            return JSONResponse(result, status_code=code)
        upstream = await session.request(
            request.method,
            f'http://{ip}:8000/{path}',
            params=params,
            headers=runtime_headers(row, request),
            data=request.stream(),
            allow_redirects=False,
        )
        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS}

        async def stream():
            try:
                async for chunk in upstream.content.iter_chunked(65536):
                    last_used[identifier] = time.monotonic()
                    yield chunk
            finally:
                upstream.close()
                await session.close()

        # Agent tools return JSON, so complete them before releasing the workspace lock.
        if agent:
            body = await upstream.read()
            await session.close()
            return Response(body, upstream.status, headers=headers)
        return StreamingResponse(stream(), upstream.status, headers=headers)
    except BaseException:
        await session.close()
        raise
    finally:
        last_used[identifier] = time.monotonic()
        if entered:
            await context.__aexit__(None, None, None)


@app.websocket('/r/{user}/{project}/{path:path}')
async def socket(ws: WebSocket, user: str, project: str, path: str):
    await ws.accept()
    tasks = []
    try:
        browser_view = path.startswith('browser/view/')
        auth = {} if browser_view else await asyncio.wait_for(ws.receive_json(), 10)
        credential = (
            ws.headers.get('authorization', '').removeprefix('Bearer ') if browser_view else str(auth.get('token', ''))
        )
        if not KEY or not secrets.compare_digest(credential, KEY):
            await ws.close(4001)
            return
        if path.startswith(('agents', 'api/agents', 'admin', 'api/admin')):
            await ws.close(4003)
            return
        row = workspace(user, project)
        if path.startswith('api/terminals/'):
            await take_manual_control(row)
        ip = await ensure(row)
        headers = runtime_headers(row, ws)
        params = dict(ws.query_params)
        params['user_id'] = user
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f'http://{ip}:8000/{path}', params=params, headers=headers, max_msg_size=16 * 1024 * 1024
            ) as upstream:
                if not browser_view:
                    await upstream.send_json({**auth, 'token': row['api_key']})

                async def incoming():
                    while True:
                        message = await ws.receive()
                        last_used[row['id']] = time.monotonic()
                        if message['type'] == 'websocket.disconnect':
                            break
                        if message.get('bytes') is not None:
                            await upstream.send_bytes(message['bytes'])
                        elif message.get('text') is not None:
                            await upstream.send_str(message['text'])

                async def outgoing():
                    async for message in upstream:
                        last_used[row['id']] = time.monotonic()
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await ws.send_text(message.data)
                        elif message.type == aiohttp.WSMsgType.BINARY:
                            await ws.send_bytes(message.data)

                tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await ws.close()


@app.post('/tools/{user}/{project}')
async def python_tool(user: str, project: str, payload: dict):
    if len(json.dumps(payload).encode()) > 2 * 1024 * 1024:
        raise HTTPException(413, 'Tool payload exceeds 2 MiB')
    if payload.get('action') not in {'discover', 'execute'}:
        raise HTTPException(400, 'Unsupported tool action')
    row = workspace(user, project)
    identifier = row['id']
    context = agent_execution(row)
    await context.__aenter__()
    job = uuid.uuid4().hex
    input_path = f'/home/user/.team-jobs/{job}.json'
    output_path = f'/home/user/.team-jobs/{job}.result'
    try:
        ip = await ensure(row)
        base = f'http://{ip}:8000'
        headers = {'Authorization': 'Bearer ' + row['api_key'], 'X-User-Id': user}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT + 10)) as session:
            form = aiohttp.FormData()
            form.add_field(
                'file',
                json.dumps({**payload, 'user_id': user}).encode(),
                filename=job + '.json',
                content_type='application/json',
            )
            async with session.post(
                base + '/files/upload', params={'directory': '/home/user/.team-jobs'}, data=form, headers=headers
            ) as response:
                if response.status != 200:
                    raise HTTPException(502, 'Unable to stage sandbox tool input')
                await response.read()
            # Only generated UUID paths enter this shell command. Source/arguments travel as JSON data.
            command = f'python /opt/team/sandbox_tool.py < {input_path} > {output_path}'
            result, code = await run_agent_command(row, session, base, headers, {'command': command})
            if code >= 400:
                raise HTTPException(code, result.get('error', 'Sandbox command failed'))
            async with session.get(base + '/files/read', params={'path': output_path}, headers=headers) as response:
                if response.status != 200:
                    raise HTTPException(422, 'Sandbox tool did not return a result')
                value = await response.json()
            try:
                output = json.loads(value['content'])
            except (ValueError, KeyError, TypeError) as error:
                raise HTTPException(422, 'Invalid sandbox tool result') from error
            if output.get('error'):
                raise HTTPException(422, output['error'])
            return output.get('result')
    finally:
        last_used[identifier] = time.monotonic()
        try:

            def cleanup():
                with client() as api:
                    runtime = container_for(api, row)
                    if runtime and runtime.status == 'running':
                        runtime.exec_run(['rm', '-f', input_path, output_path], user='1000:1000')

            await asyncio.to_thread(cleanup)
        finally:
            await context.__aexit__(None, None, None)

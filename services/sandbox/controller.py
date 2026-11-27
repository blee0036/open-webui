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
import shutil
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


def parse_bytes(value):
    """'2g', '512m', '1.5G' or a plain byte count -> bytes."""
    text = str(value).strip().lower().removesuffix('b').removesuffix('i')
    units = {'k': 1024, 'm': 1024**2, 'g': 1024**3, 't': 1024**4}
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text or 0)


# Per-workspace limits. Swap comes from the host's swap space (a swap file on whichever disk
# the host configured); SANDBOX_SWAP only caps how much of it one workspace may use.
MEMORY = parse_bytes(os.environ.get('SANDBOX_MEMORY', '2g'))
SWAP = parse_bytes(os.environ.get('SANDBOX_SWAP', '0'))
CPUS = float(os.environ.get('SANDBOX_CPUS', '1'))
if MEMORY < 256 * 1024**2 or SWAP < 0 or CPUS <= 0:
    raise RuntimeError('SANDBOX_MEMORY must be at least 256m, SANDBOX_SWAP >= 0 and SANDBOX_CPUS > 0')
TIMEOUT = int(os.environ.get('SANDBOX_EXECUTION_SECONDS', '1800'))
# open-terminal accepts at most 300 seconds per status wait, so longer runs are polled.
STATUS_WAIT_LIMIT = 300
# How long an Agent command may wait for a free slot or for this workspace's previous command.
# WebUI has no tool-call timeout by default, so an unbounded wait looks like a hung command.
AGENT_WAIT = float(os.environ.get('SANDBOX_AGENT_WAIT_SECONDS', '60'))
MONITOR_SECONDS = float(os.environ.get('SANDBOX_MONITOR_SECONDS', '20'))
# Home volumes have no kernel quota; their sizes are measured this often (a soft limit).
DISK_SCAN_SECONDS = float(os.environ.get('SANDBOX_DISK_SCAN_SECONDS', '300'))
state_dir = Path(os.environ.get('SANDBOX_STATE_DIR', '/state'))
state_dir.mkdir(parents=True, exist_ok=True)
state = sqlite3.connect(state_dir / 'workspaces.db')
state.row_factory = sqlite3.Row
state.execute("""CREATE TABLE IF NOT EXISTS workspace (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, project_id TEXT NOT NULL, api_key TEXT NOT NULL,
    public INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'stopped', error TEXT,
    UNIQUE(user_id, project_id))""")
# Last answers to the read-only probes the UI and WebUI send before anyone uses a workspace.
state.execute("""CREATE TABLE IF NOT EXISTS probe_cache (
    scope TEXT NOT NULL, path TEXT NOT NULL, status INTEGER NOT NULL, body BLOB NOT NULL,
    PRIMARY KEY(scope, path))""")
# Per-user sandbox storage caps pushed by WebUI from each member's package; absent = unlimited.
state.execute('CREATE TABLE IF NOT EXISTS user_limit (user_id TEXT PRIMARY KEY, sandbox_bytes INTEGER NOT NULL)')
state.commit()
# Last measured home-volume size per workspace id, and when the scan finished (epoch seconds).
disk_usage = {}
disk_scanned_at = None
locks = {}
lifecycle_locks = {}
resumed = {}
agent_waiters = {}
agent_running = set()
manual = set()
# Why a workspace is under manual control: the user chose "Take manual control" (pinned, until
# they resume or it stops), or a terminal or manual command is in flight (released when it ends).
pinned = set()
manual_holders = {}
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


class Busy(Exception):
    """An Agent command that cannot run now; the message is returned to the model."""


MANUAL_MESSAGE = (
    'The user has taken manual control of this workspace, so Agent commands are paused. '
    'Ask the user to choose "Resume Agent queue" on the Work environment page, then retry.'
)


def format_bytes(value):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(value) < 1024 or unit == 'TB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024


def disk_state(user_id):
    """The member's measured sandbox storage across all their workspaces against their cap."""
    ids = [row[0] for row in state.execute('SELECT id FROM workspace WHERE user_id=?', (user_id,))]
    used = sum(disk_usage.get(identifier, 0) for identifier in ids)
    limit_row = state.execute('SELECT sandbox_bytes FROM user_limit WHERE user_id=?', (user_id,)).fetchone()
    limit = limit_row[0] if limit_row else None
    return {
        'used': used,
        'limit': limit,
        'over': limit is not None and disk_scanned_at is not None and used > limit,
        'scanned_at': disk_scanned_at,
    }


def quota_message(disk):
    return (
        f'Sandbox storage is full: {format_bytes(disk["used"])} used of {format_bytes(disk["limit"])} '
        'across your workspaces, so Agent commands are paused. Tell the user to delete files in the '
        'workspace terminal or file panel (or clear the workspace), or to ask an administrator for more '
        'space. Agent commands resume after the next storage check.'
    )


def scan_volumes():
    """Measured size of every home volume this controller owns, keyed by workspace id."""
    prefix, suffix = NAMESPACE + '-ws-', '-home'
    sizes = {}
    with client() as api:
        for volume in api.df().get('Volumes') or []:
            volume_name = volume.get('Name') or ''
            labels = volume.get('Labels') or {}
            if not (volume_name.startswith(prefix) and volume_name.endswith(suffix)):
                continue
            if any(labels.get(key) != value for key, value in LABEL.items()):
                continue
            size = (volume.get('UsageData') or {}).get('Size', -1)
            sizes[volume_name[len(prefix) : -len(suffix)]] = max(0, int(size if size is not None else 0))
    return sizes


async def disk_monitor():
    global disk_scanned_at
    while True:
        try:
            # docker df walks every volume; keep it off the event loop and out of request paths.
            disk_usage.clear()
            disk_usage.update(await asyncio.to_thread(scan_volumes))
            disk_scanned_at = int(time.time())
        except Exception as error:
            print(f'Sandbox storage scan failed: {error}', flush=True)
        await asyncio.sleep(DISK_SCAN_SECONDS)


@asynccontextmanager
async def agent_execution(row):
    identifier = row['id']
    disk = disk_state(row['user_id'])
    if disk['over']:
        # Manual terminals and file deletion keep working, so the user can clean up.
        raise Busy(quota_message(disk))
    if identifier in manual:
        raise Busy(MANUAL_MESSAGE)
    lock = locks.setdefault(identifier, asyncio.Lock())
    agent_waiters[identifier] = agent_waiters.get(identifier, 0) + 1
    acquired = False
    try:
        try:
            async with asyncio.timeout(AGENT_WAIT):
                while True:
                    await resume_event(identifier).wait()
                    await lock.acquire()
                    if identifier not in manual:
                        acquired = True
                        break
                    lock.release()
        except TimeoutError:
            if identifier in manual:
                raise Busy(MANUAL_MESSAGE) from None
            raise Busy(
                f'Another command in this workspace is still running after {AGENT_WAIT:g} seconds. Retry later.'
            ) from None
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


def release_manual_control(identifier):
    pinned.discard(identifier)
    manual.discard(identifier)
    resume_event(identifier).set()


async def hold_manual_control(row):
    """Pause Agent commands while a terminal or manual command is in use."""
    manual_holders[row['id']] = manual_holders.get(row['id'], 0) + 1
    try:
        await take_manual_control(row)
    except BaseException:
        drop_manual_control(row['id'])
        raise


def drop_manual_control(identifier):
    manual_holders[identifier] = max(0, manual_holders.get(identifier, 0) - 1)
    if not manual_holders[identifier] and identifier not in pinned:
        release_manual_control(identifier)


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
    host = runtime.attrs.get('HostConfig') or {}
    return (
        BROWSER_ENV in ((runtime.attrs.get('Config') or {}).get('Env') or [])
        and host.get('Memory') == MEMORY
        and host.get('MemorySwap') == MEMORY + SWAP
        and host.get('NanoCpus') == int(CPUS * 1_000_000_000)
    )


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
                nano_cpus=int(CPUS * 1_000_000_000),
                mem_limit=MEMORY,
                # memory + swap; equal to mem_limit means no swap.
                memswap_limit=MEMORY + SWAP,
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


async def ensure(row, wait=None):
    # Exactly one waiter per workspace may enter the global capacity queue.
    async with lifecycle(row):
        return await ensure_locked(row, wait)


async def ensure_locked(row, wait=None):
    """Start the runtime; `wait` bounds the time spent queued for a free slot."""
    identifier = row['id']
    last_used[identifier] = time.monotonic()
    async with capacity:
        if identifier not in active:
            if identifier not in queue:
                queue.append(identifier)
            set_status(row, 'queued')
            try:
                async with asyncio.timeout(wait):
                    await capacity.wait_for(lambda: len(active) < MAX_RUNNING and queue and queue[0] == identifier)
                queue.pop(0)
                active.add(identifier)
                capacity.notify_all()
            except BaseException as error:
                if identifier in queue:
                    queue.remove(identifier)
                    capacity.notify_all()
                set_status(row, 'stopped')
                if isinstance(error, TimeoutError):
                    raise Busy(
                        f'All {MAX_RUNNING} sandboxes are in use and none became free within {wait:g} seconds. '
                        'Retry later.'
                    ) from None
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


# ---------------------------------------------------------------------------
# On-demand start: read-only probes never start a runtime.
#
# Opening a chat makes the UI ask for features, skills and the working directory, and every
# message makes WebUI read the tool spec and AGENTS.md. A stopped workspace answers these from
# the last values seen while it ran. They cannot go stale while it is stopped: only the
# runtime writes to the home volume, and the cache is refreshed just before each stop.
# Commands, terminals, file browsing and uploads still start the runtime.
# ---------------------------------------------------------------------------
HOME = '/home/user'
IMAGE_SCOPE = f'image:{IMAGE}:{BROWSER_ENV}'
IMAGE_PROBES = {'openapi.json', 'api/config'}  # Same for every runtime of this image.
# Literally true of a stopped runtime.
STOPPED_ANSWERS = {'ports': {'ports': []}, 'api/terminals': [], 'browser/sessions': []}
DEFAULT_PROBES = ('skills', 'files/cwd')


def probe_key(path, request):
    """Cache key for a read-only probe, or None for a request that needs the runtime."""
    if request.method != 'GET':
        return None
    query = request.url.query
    if path in IMAGE_PROBES or path in STOPPED_ANSWERS or (path in DEFAULT_PROBES and not query):
        return path
    if path == 'files/read' and str(request.query_params.get('path', '')).endswith('/AGENTS.md'):
        return f'{path}?{query}'
    return None


def probe_scope(row, key):
    return IMAGE_SCOPE if key in IMAGE_PROBES else row['id']


def cache_get(scope, key):
    return state.execute('SELECT status, body FROM probe_cache WHERE scope=? AND path=?', (scope, key)).fetchone()


def cache_put(scope, key, status, body):
    state.execute(
        'INSERT OR REPLACE INTO probe_cache (scope, path, status, body) VALUES (?,?,?,?)', (scope, key, status, body)
    )
    state.commit()


def forget_workspace_probes(identifier):
    state.execute('DELETE FROM probe_cache WHERE scope=?', (identifier,))
    state.commit()


def stopped_answer(row, key):
    """Answer for a stopped runtime, or None when it has to start (first run of this image)."""
    if key in STOPPED_ANSWERS:
        return Response(json.dumps(STOPPED_ANSWERS[key]), 200, media_type='application/json')
    cached = cache_get(probe_scope(row, key), key)
    if cached is not None:
        return Response(cached['body'], cached['status'], media_type='application/json')
    if key in IMAGE_PROBES:
        return None
    if key == 'skills':
        body, status = [], 200
    elif key == 'files/cwd':
        body, status = {'cwd': HOME, 'home': HOME, 'root': {'path': HOME, 'label': 'Home'}}, 200
    else:  # AGENTS.md in a workspace that has never run
        body, status = {'detail': 'File not found'}, 404
    return Response(json.dumps(body), status, media_type='application/json')


async def fetch_probe(row, key, headers):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.get(f'http://{row["ip"]}:8000/{key}', headers=headers, allow_redirects=False) as upstream:
            return upstream.status, await upstream.read()


async def running_probe(row, key, request):
    """Forward a probe to a running runtime without counting it as use, and remember the answer."""
    headers = runtime_headers(row, request)
    status, body = await fetch_probe(row, key, headers)
    # A session's cwd is not what a restarted runtime reports, so only sessionless answers are kept.
    if key not in STOPPED_ANSWERS and status in (200, 404) and not (key == 'files/cwd' and 'x-session-id' in headers):
        cache_put(probe_scope(row, key), key, status, body)
    return Response(body, status, media_type='application/json')


async def refresh_probes(row):
    """Re-read this workspace's probes from the runtime right before it stops."""
    if not row.get('ip'):
        return
    keys = {r['path'] for r in state.execute('SELECT path FROM probe_cache WHERE scope=?', (row['id'],))}
    headers = {'Authorization': 'Bearer ' + row['api_key'], 'X-User-Id': row['user_id']}
    for key in keys | set(DEFAULT_PROBES):
        try:
            status, body = await fetch_probe(row, key, headers)
            if status not in (200, 404):
                raise RuntimeError(status)
            cache_put(row['id'], key, status, body)
        except Exception:
            # Better to answer "not found" than to serve a stale file.
            state.execute('DELETE FROM probe_cache WHERE scope=? AND path=?', (row['id'], key))
            state.commit()


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
                    await refresh_probes(row)
                    await docker_call(stop_runtime, row)
                    row.pop('ip', None)
                    set_status(row, 'stopped')
                    release_manual_control(identifier)
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
            # Stopped without a final refresh, so its probe answers may be stale.
            forget_workspace_probes(container.name.removeprefix(NAMESPACE + '-ws-'))
            await asyncio.to_thread(container.stop, timeout=5)
    state.execute("UPDATE workspace SET status='stopped', error=NULL")
    state.commit()
    task = asyncio.create_task(idle_monitor())
    disk = asyncio.create_task(disk_monitor())
    # Pull a new image now rather than inside the first start, which holds the global Docker lock.
    pull = asyncio.create_task(asyncio.to_thread(prepull_images))
    try:
        yield
    finally:
        for background in (task, disk, pull):
            background.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background


def prepull_images():
    try:
        with client() as api:
            for image in (IMAGE, HELPER_IMAGE):
                ensure_image(api, image)
    except Exception as error:  # The first start retries the pull.
        print(f'Sandbox image pre-pull failed: {error}', flush=True)


app = FastAPI(lifespan=lifespan, dependencies=[Depends(authenticate)])


@app.exception_handler(Busy)
async def busy_handler(request, error):
    # Returned to the model and the UI as-is, so it must read as an instruction to a person.
    return JSONResponse({'status': 'busy', 'error': str(error), 'detail': str(error)}, status_code=503)


@app.get('/health')
async def health():
    return {'running': len(active), 'queued': len(queue), 'limit': MAX_RUNNING}


def workspace_state(row):
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


@app.get('/workspaces/{user}/{project}')
async def status(user: str, project: str):
    row = workspace(user, project)
    return workspace_state(row) | {
        'disk': disk_state(user) | {'workspace_used': disk_usage.get(row['id'], 0)},
    }


class Control(BaseModel):
    action: str
    confirm: str | None = None


RESCAN_INTERVAL = 30
last_rescan = 0.0


async def rescan_now():
    """Re-measure on request (after the user cleaned up), at most once per RESCAN_INTERVAL."""
    global disk_scanned_at, last_rescan
    if time.monotonic() - last_rescan < RESCAN_INTERVAL:
        return
    last_rescan = time.monotonic()
    sizes = await asyncio.to_thread(scan_volumes)
    disk_usage.clear()
    disk_usage.update(sizes)
    disk_scanned_at = int(time.time())


@app.post('/workspaces/{user}/{project}')
async def control(user: str, project: str, form: Control):
    if form.action == 'rescan':
        with contextlib.suppress(Exception):
            await rescan_now()
        return await status(user, project)
    await apply_control(workspace(user, project), form)
    return await status(user, project)


async def apply_control(row, form):
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
            pinned.add(identifier)
            await take_manual_control(row)
        else:
            release_manual_control(identifier)
    elif form.action in {'stop', 'recreate', 'offline', 'public', 'clear'}:
        if form.action == 'clear' and form.confirm != 'CLEAR MY WORKSPACE':
            raise HTTPException(400, 'Explicit clear confirmation required')
        if task := starters.get(identifier):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        async with lock, lifecycle(row), docker_lock:
            await refresh_probes(row)
            await docker_call(stop_runtime, row, form.action != 'stop')
            if form.action == 'clear':
                forget_workspace_probes(identifier)
                with client() as api:
                    try:
                        volume = api.volumes.get(name(row) + '-home')
                    except docker.errors.NotFound:
                        volume = None
                    if volume:
                        if any(volume.attrs.get('Labels', {}).get(k) != v for k, v in LABEL.items()):
                            raise HTTPException(409, 'Volume ownership mismatch')
                        await docker_call(volume.remove)
                # The volume is gone; do not keep blocking the Agent until the next scan.
                disk_usage[identifier] = 0
            if form.action in {'public', 'offline'}:
                row['public'] = int(form.action == 'public')
                state.execute('UPDATE workspace SET public=? WHERE id=?', (row['public'], identifier))
            row.pop('ip', None)
            set_status(row, 'stopped')
            # Its terminals are gone with the runtime, so nothing is left to hand back.
            release_manual_control(identifier)
            async with capacity:
                active.discard(identifier)
                capacity.notify_all()
    else:
        raise HTTPException(400, 'Unknown workspace action')


# ---------------------------------------------------------------------------
# Administration (WebUI admins only; WebUI holds the controller key and checks the role).
# ---------------------------------------------------------------------------
ADMIN_ACTIONS = {'stop', 'recreate', 'clear'}


class Limits(BaseModel):
    limits: dict[str, int | None]


@app.put('/admin/limits')
async def set_limits(form: Limits):
    """Replace every member's sandbox storage cap (idempotent; WebUI pushes the full map)."""
    rows = [(user_id, int(value)) for user_id, value in form.limits.items() if value is not None and value >= 0]
    state.execute('DELETE FROM user_limit')
    state.executemany('INSERT INTO user_limit (user_id, sandbox_bytes) VALUES (?,?)', rows)
    state.commit()
    return {'count': len(rows)}


@app.get('/admin/users/{user_id}/disk')
async def user_disk(user_id: str):
    """Read-only: never creates a workspace row (the quota badge polls this)."""
    return disk_state(user_id)


def container_memory():
    """Memory use of this controller's running runtimes, keyed by workspace id."""
    prefix = NAMESPACE + '-ws-'
    usage = {}
    with client() as api:
        filters = {'label': [f'{key}={value}' for key, value in LABEL.items()], 'status': 'running'}
        for container in api.containers.list(filters=filters):
            if not container.name.startswith(prefix):
                continue
            try:
                stats = container.stats(stream=False)
            except Exception:
                continue
            memory = stats.get('memory_stats') or {}
            # Page cache is reclaimable; report what the kernel would actually need to keep.
            cache = (memory.get('stats') or {}).get('inactive_file', 0)
            usage[container.name.removeprefix(prefix)] = {
                'used': max(0, memory.get('usage', 0) - cache),
                'limit': memory.get('limit'),
            }
    return usage


@app.get('/admin/workspaces')
async def admin_workspaces():
    try:
        memory = await asyncio.wait_for(asyncio.to_thread(container_memory), 20)
    except Exception:
        memory = {}
    now = time.monotonic()
    result = []
    for db_row in state.execute('SELECT * FROM workspace ORDER BY user_id, project_id').fetchall():
        db_row = dict(db_row)
        # The cached row is the live one (it carries the runtime address); fall back to the stored state.
        row = workspaces.get((db_row['user_id'], db_row['project_id']), db_row)
        identifier = row['id']
        result.append(
            workspace_state(row)
            | {
                'user_id': row['user_id'],
                'project_id': row['project_id'],
                'disk': disk_usage.get(identifier),
                'memory': memory.get(identifier),
                'idle_seconds': int(now - last_used[identifier]) if identifier in last_used else None,
            }
        )
    users = {item['user_id'] for item in result}
    return {
        'workspaces': result,
        'users': {user_id: disk_state(user_id) for user_id in users},
        'scanned_at': disk_scanned_at,
        'scan_interval': DISK_SCAN_SECONDS,
        'running': len(active),
        'queued': len(queue),
        'limit': MAX_RUNNING,
        'memory_per_workspace': MEMORY,
    }


def host_info():
    with client() as api:
        info = api.info()
        df = api.df()
    volumes = [v for v in df.get('Volumes') or [] if all((v.get('Labels') or {}).get(k) == x for k, x in LABEL.items())]
    disk = shutil.disk_usage(state_dir)
    return {
        'docker_version': info.get('ServerVersion'),
        'cpus': info.get('NCPU'),
        'memory_total': info.get('MemTotal'),
        'docker_root': info.get('DockerRootDir'),
        'images_size': df.get('LayersSize'),
        'sandbox_volumes': len(volumes),
        'sandbox_volumes_size': sum(max(0, (v.get('UsageData') or {}).get('Size') or 0) for v in volumes),
        # The controller's state volume lives on the Docker data disk, so this is that disk's free space.
        'disk': {'total': disk.total, 'used': disk.used, 'free': disk.free},
    }


@app.get('/admin/host')
async def admin_host():
    try:
        host = await asyncio.wait_for(asyncio.to_thread(host_info), 30)
    except Exception as error:
        raise HTTPException(503, f'Docker is unavailable: {error}') from error
    return host | {'running': len(active), 'queued': len(queue), 'limit': MAX_RUNNING}


@app.post('/admin/workspaces/{identifier}')
async def admin_control(identifier: str, form: Control):
    if form.action not in ADMIN_ACTIONS:
        raise HTTPException(400, 'Unknown administrator action')
    db_row = state.execute('SELECT user_id, project_id FROM workspace WHERE id=?', (identifier,)).fetchone()
    if db_row is None:
        raise HTTPException(404, 'Workspace not found')
    row = workspace(db_row['user_id'], db_row['project_id'])
    await apply_control(row, form)
    return workspace_state(row) | {'user_id': row['user_id'], 'project_id': row['project_id']}


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
        await refresh_probes(row)
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
            tail = {k: v for k, v in (query or {}).items() if k == 'tail'}
            # The enclosing asyncio.timeout(TIMEOUT) ends the loop for a command that never finishes.
            while result.get('status') == 'running':
                params = {'wait': str(STATUS_WAIT_LIMIT), **tail}
                async with session.get(
                    f'{base}/execute/{process_id}/status', params=params, headers=headers
                ) as response:
                    result = await response.json()
                    if response.status >= 400:
                        raise RuntimeError('Runtime could not confirm command completion')
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
    if (key := probe_key(path, request)) is not None:
        if row.get('ip'):
            with contextlib.suppress(aiohttp.ClientError, TimeoutError):
                return await running_probe(row, key, request)
        if (answer := stopped_answer(row, key)) is not None:
            return answer
    agent = request.headers.get('x-agent-execution') == '1'
    held = not agent and request.method == 'POST' and path in {'execute', 'api/terminals/user/input'}
    if held:
        await hold_manual_control(row)
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT + 15))
    context = agent_execution(row) if agent else None
    entered = False
    try:
        if agent:
            try:
                await context.__aenter__()
                entered = True
                ip = await ensure(row, AGENT_WAIT)
            except Busy as busy:
                await session.close()
                return JSONResponse({'status': 'busy', 'error': str(busy)}, status_code=503)
        else:
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
        if held:
            drop_manual_control(identifier)


@app.websocket('/r/{user}/{project}/{path:path}')
async def socket(ws: WebSocket, user: str, project: str, path: str):
    await ws.accept()
    tasks = []
    held = None
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
            await hold_manual_control(row)
            held = row['id']
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
        if held:
            drop_manual_control(held)
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

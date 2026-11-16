"""Opt-in Docker acceptance test. Builds must already exist; uses only a unique test namespace.

Run: python -m pytest services/sandbox/tests -q -s
The fixture removes only the containers/networks/volumes it creates.
"""

import concurrent.futures
import io
import json
import os
import secrets
import subprocess
import time
import uuid
import zipfile

import pytest
import requests


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', check=True)
    return result.stdout.strip()


@pytest.fixture(scope='module')
def controller(request):
    namespace = 'owui-it-' + uuid.uuid4().hex[:8]
    controller_name, proxy_name = namespace + '-sandbox', namespace + '-egress'
    key = secrets.token_hex(32)
    env = {**os.environ, 'SANDBOX_CONTROLLER_KEY': key}
    options = getattr(request, 'param', {})
    network, volume = namespace + '-control', namespace + '-state'
    docker('network', 'create', network)
    docker('volume', 'create', volume)
    try:
        docker(
            'run',
            '-d',
            '--name',
            proxy_name,
            '--network',
            network,
            '-e',
            'SANDBOX_EGRESS_DENY_HOSTS=platform.example.com',
            'open-webui-sandbox-controller:1',
            'python',
            '/app/public_proxy.py',
        )
        subprocess.run(
            [
                'docker',
                'run',
                '-d',
                '--name',
                controller_name,
                '--network',
                network,
                '-p',
                '127.0.0.1::8080',
                '-e',
                'SANDBOX_CONTROLLER_KEY',
                '-e',
                f'SANDBOX_NAMESPACE={namespace}',
                '-e',
                f'SANDBOX_CONTROLLER_CONTAINER={controller_name}',
                '-e',
                f'SANDBOX_PROXY_CONTAINER={proxy_name}',
                '-e',
                'SANDBOX_EXECUTION_SECONDS=8',
                '-e',
                f'SANDBOX_IDLE_SECONDS={options.get("idle", 1800)}',
                '-e',
                # The queue test fills exactly this many slots; keep it independent of the default.
                f'SANDBOX_MAX_RUNNING={options.get("max_running", 4)}',
                '-e',
                f'SANDBOX_MONITOR_SECONDS={options.get("monitor", 20)}',
                '-v',
                '/var/run/docker.sock:/var/run/docker.sock',
                '-v',
                f'{volume}:/state',
                'open-webui-sandbox-controller:1',
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        port = json.loads(docker('inspect', controller_name))[0]['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
        base = f'http://127.0.0.1:{port}'
        session = requests.Session()
        session.trust_env = False
        session.headers['Authorization'] = 'Bearer ' + key
        for _ in range(60):
            try:
                if session.get(base + '/health', timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        else:
            pytest.fail('Controller startup failed: ' + docker('logs', controller_name))

        def call(method, path, *, allowed=(200,), **kwargs):
            response = session.request(method, base + path, timeout=kwargs.pop('timeout', 150), **kwargs)
            assert response.status_code in allowed, (
                path,
                response.status_code,
                response.text[:2000],
                docker('logs', controller_name)[-2000:],
            )
            return response

        def refresh_address():
            nonlocal base
            # Docker may choose a new dynamic host port after a container restart.
            port = json.loads(docker('inspect', controller_name))[0]['NetworkSettings']['Ports']['8080/tcp'][0][
                'HostPort'
            ]
            base = f'http://127.0.0.1:{port}'

        call.refresh_address = refresh_address

        yield call, namespace
    finally:
        # Stop the controller first so its idle monitor cannot race teardown.
        subprocess.run(['docker', 'rm', '-f', controller_name], capture_output=True)
        for identifier in docker('ps', '-aq', '--filter', f'label=owner={namespace}').splitlines():
            docker('rm', '-f', identifier)
        subprocess.run(['docker', 'rm', '-f', proxy_name], capture_output=True)
        for identifier in docker('network', 'ls', '-q', '--filter', f'label=owner={namespace}').splitlines():
            docker('network', 'rm', identifier)
        docker('network', 'rm', network)
        for identifier in docker('volume', 'ls', '-q', '--filter', f'label=owner={namespace}').splitlines():
            docker('volume', 'rm', identifier)
        docker('volume', 'rm', volume)


def wait_running(call, user='alice', project='project-a'):
    path = f'/workspaces/{user}/{project}'
    result = call('POST', path, json={'action': 'start'}).json()
    for _ in range(120):
        result = call('GET', path).json()
        if result['status'] in {'running', 'failed'}:
            break
        time.sleep(0.5)
    assert result['status'] == 'running', result
    return result


def execute(call, command, user='alice', project='project-a', allowed=(200,)):
    value = call(
        'POST',
        f'/r/{user}/{project}/execute',
        json={'command': command},
        headers={'X-Agent-Execution': '1'},
        allowed=allowed,
    ).json()
    if isinstance(value.get('output'), list):
        value['output'] = ''.join(item.get('data', '') for item in value['output'] if item.get('type') == 'output')
    return value


def test_upload_python_artifacts_persistence_isolation_and_limits(controller):
    call, namespace = controller
    workspace = wait_running(call)
    prefix = '/r/alice/project-a'
    call(
        'POST',
        prefix + '/files/upload',
        params={'directory': '/home/user'},
        files={'file': ('data.csv', b'month,value\nJan,10\nFeb,20\n', 'text/csv')},
    )
    result = execute(
        call,
        "python - <<'PY'\nimport os, pandas as pd\nimport matplotlib; matplotlib.use('Agg')\nimport matplotlib.pyplot as plt\ndf=pd.read_csv('/home/user/data.csv')\ndf.to_excel('/home/user/result.xlsx',index=False)\nplt.plot(df['month'],df['value']); plt.savefig('/home/user/chart.png')\nprint('uid='+str(os.getuid()))\nprint('secret='+str(os.getenv('SANDBOX_CONTROLLER_KEY')))\nprint('socket='+str(os.path.exists('/var/run/docker.sock')))\nPY",
    )
    assert result['exit_code'] == 0, result
    assert 'uid=1000' in result['output'] and 'secret=None' in result['output'] and 'socket=False' in result['output']
    png = call('GET', prefix + '/files/read', params={'path': '/home/user/chart.png'})
    assert png.content.startswith(b'\x89PNG')
    spreadsheet = call('GET', prefix + '/files/read', params={'path': '/home/user/result.xlsx'})
    # Read API extracts spreadsheets for preview; download API returns original bytes.
    assert spreadsheet.status_code == 200
    original = call('GET', prefix + '/files/view', params={'path': '/home/user/result.xlsx'})
    assert zipfile.is_zipfile(io.BytesIO(original.content))
    runtime = json.loads(docker('inspect', namespace + '-ws-' + workspace['id']))[0]
    limits = runtime['HostConfig']
    assert limits['Memory'] == 2 * 1024**3 and limits['NanoCpus'] == 1_000_000_000
    assert limits['PidsLimit'] == 128 and limits['ReadonlyRootfs'] and limits['CapDrop'] == ['ALL']
    for action in ('stop', 'recreate'):
        call('POST', '/workspaces/alice/project-a', json={'action': action})
        wait_running(call)
        assert 'Jan,10' in call('GET', prefix + '/files/read', params={'path': '/home/user/data.csv'}).json()['content']
    for user, project in (('bob', 'project-a'), ('alice', 'project-b')):
        wait_running(call, user, project)
        call('GET', f'/r/{user}/{project}/files/read', params={'path': '/home/user/data.csv'}, allowed=(404,))
    print(
        'PASS upload → Python → PNG/XLSX preview/download; user/project isolation; stop/recreate persistence; resource limits'
    )


def test_offline_public_egress_and_timeout(controller):
    call, _ = controller
    wait_running(call)
    offline = execute(
        call,
        "python - <<'PY'\nimport urllib.request\ntry: urllib.request.urlopen('https://example.com',timeout=2); print('LEAK')\nexcept Exception: print('BLOCKED')\nPY",
    )
    assert 'BLOCKED' in offline['output']
    call('POST', '/workspaces/alice/project-a', json={'action': 'public'})
    wait_running(call)
    public = execute(
        call,
        "python - <<'PY'\nimport urllib.request\nprint(urllib.request.urlopen('https://example.com',timeout=5).status)\nfor url in ['http://169.254.169.254','http://127.0.0.1:8080','https://platform.example.com']:\n try: urllib.request.urlopen(url,timeout=2); print('LEAK')\n except Exception: print('BLOCKED')\nPY",
    )
    assert public['exit_code'] == 0 and '200' in public['output'] and public['output'].count('BLOCKED') == 3, public
    timeout = execute(call, 'sleep 30', allowed=(408,))
    assert timeout['status'] == 'timeout'
    assert call('GET', '/workspaces/alice/project-a').json()['status'] == 'timeout'
    print('PASS offline/public boundary, metadata/control-plane denial, timeout stops all processes and retains files')


def test_manual_queue_sandbox_tool_and_capacity(controller):
    call, _ = controller
    wait_running(call)
    call('POST', '/workspaces/alice/project-a', json={'action': 'pause'})
    with concurrent.futures.ThreadPoolExecutor() as pool:
        job = pool.submit(execute, call, 'printf resumed')
        time.sleep(0.6)
        state = call('GET', '/workspaces/alice/project-a').json()
        assert state['agent_status'] == 'paused' and state['agent_queue'] == 1 and not job.done()
        call('POST', '/workspaces/alice/project-a', json={'action': 'resume'})
        assert 'resumed' in job.result()['output']
    source = 'class Tools:\n    def add(self, a: int, b: int) -> int:\n        return a+b\n'
    payload = {'action': 'discover', 'tool_id': 'test', 'content': source}
    assert call('POST', '/tools/alice/project-a', json=payload).json()['specs'][0]['name'] == 'add'
    payload.update(action='execute', function='add', params={'a': 2, 'b': 3})
    assert call('POST', '/tools/alice/project-a', json=payload).json() == 5
    # Three workspaces already exist from the first test; fill four, queue the fifth.
    wait_running(call, 'four', 'personal')
    call('POST', '/workspaces/five/personal', json={'action': 'start'})
    time.sleep(0.5)
    health = call('GET', '/health').json()
    assert health['running'] == 4 and health['queued'] == 1, health
    call('POST', '/workspaces/bob/project-a', json={'action': 'stop'})
    wait_running(call, 'five', 'personal')
    print('PASS manual takeover/Agent queue, sandbox discovery/execution, global four-container limit and FIFO start')


def test_controller_restart_recovers_volume_and_scope(controller):
    call, namespace = controller
    identifier = call('GET', '/workspaces/alice/project-a').json()['id']
    docker('restart', namespace + '-sandbox')
    call.refresh_address()
    for _ in range(60):
        try:
            health = call('GET', '/health', timeout=2).json()
            break
        except requests.RequestException:
            time.sleep(0.25)
    else:
        pytest.fail('Controller did not recover after restart')
    assert health['running'] == 0
    restored = wait_running(call)
    assert restored['id'] == identifier
    assert (
        'Jan,10'
        in call('GET', '/r/alice/project-a/files/read', params={'path': '/home/user/data.csv'}).json()['content']
    )
    print('PASS controller restart preserves scope identity and persistent files, without retaining running processes')


@pytest.mark.parametrize('controller', [{'idle': 3, 'monitor': 1}], indirect=True)
def test_idle_shutdown_and_resume_keep_files(controller):
    call, _ = controller
    wait_running(call)
    execute(call, 'printf persistent > /home/user/idle-check.txt')
    for _ in range(24):
        state = call('GET', '/workspaces/alice/project-a').json()
        if state['status'] == 'stopped':
            break
        time.sleep(0.5)
    assert state['status'] == 'stopped', state
    wait_running(call)
    assert (
        call('GET', '/r/alice/project-a/files/read', params={'path': '/home/user/idle-check.txt'}).json()['content']
        == 'persistent'
    )
    print('PASS idle shutdown and resume preserve files through the production lifecycle path')

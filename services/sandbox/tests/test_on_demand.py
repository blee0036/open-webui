"""On-demand start and resource limits; no Docker required."""

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

docker = pytest.importorskip('docker')
from starlette.requests import Request  # noqa: E402


def controller(monkeypatch, **env):
    monkeypatch.setenv('SANDBOX_STATE_DIR', tempfile.mkdtemp())
    monkeypatch.setenv('SANDBOX_CONTROLLER_KEY', 'test-key')
    for key in ('SANDBOX_MEMORY', 'SANDBOX_SWAP', 'SANDBOX_CPUS', 'SANDBOX_AGENT_WAIT_SECONDS'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    path = Path(__file__).parents[1] / 'controller.py'
    spec = importlib.util.spec_from_file_location('team_test_on_demand_' + str(sorted(env.items())), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def request(method='GET', query=''):
    return Request({'type': 'http', 'method': method, 'path': '/', 'query_string': query.encode(), 'headers': []})


def answer(module, row, path, query=''):
    key = module.probe_key(path, request(query=query))
    assert key is not None, path
    response = module.stopped_answer(row, key)
    return response and (response.status_code, json.loads(response.body))


def test_probes_of_a_never_started_workspace_do_not_start_it(monkeypatch):
    module = controller(monkeypatch)
    row = module.get_workspace('u1', 'personal')
    assert answer(module, row, 'skills') == (200, [])
    assert answer(module, row, 'files/cwd')[1]['home'] == '/home/user'
    assert answer(module, row, 'files/read', 'path=%2Fhome%2Fuser%2FAGENTS.md')[0] == 404
    assert answer(module, row, 'ports') == (200, {'ports': []})
    assert answer(module, row, 'api/terminals') == (200, [])
    # The tool spec is learned from the first runtime of this image, which has to start once.
    assert answer(module, row, 'openapi.json') is None


def test_cached_answers_are_served_while_stopped(monkeypatch):
    module = controller(monkeypatch)
    row = module.get_workspace('u1', 'personal')
    other = module.get_workspace('u2', 'personal')
    module.cache_put(module.IMAGE_SCOPE, 'openapi.json', 200, b'{"paths": {}}')
    module.cache_put(row['id'], 'skills', 200, b'[{"name": "mine"}]')
    agents = 'files/read?path=%2Fhome%2Fuser%2FAGENTS.md'
    module.cache_put(row['id'], agents, 200, b'{"content": "rules"}')

    assert answer(module, other, 'openapi.json') == (200, {'paths': {}})  # Image-wide.
    assert answer(module, row, 'skills') == (200, [{'name': 'mine'}])
    assert answer(module, other, 'skills') == (200, [])  # Never shared between workspaces.
    assert answer(module, row, 'files/read', agents.split('?', 1)[1]) == (200, {'content': 'rules'})

    module.forget_workspace_probes(row['id'])
    assert answer(module, row, 'skills') == (200, [])


def test_real_work_still_starts_the_runtime(monkeypatch):
    module = controller(monkeypatch)
    assert module.probe_key('execute', request('POST')) is None
    assert module.probe_key('files/list', request(query='directory=%2F')) is None
    assert module.probe_key('files/read', request(query='path=%2Fhome%2Fuser%2Fdata.csv')) is None
    assert module.probe_key('skills/read', request(query='name=x')) is None
    assert module.probe_key('files/cwd', request('POST')) is None


def test_memory_swap_and_cpu_settings(monkeypatch):
    default = controller(monkeypatch)
    assert (default.MEMORY, default.SWAP, default.CPUS) == (2 * 1024**3, 0, 1.0)
    module = controller(monkeypatch, SANDBOX_MEMORY='4g', SANDBOX_SWAP='8G', SANDBOX_CPUS='2')
    assert (module.MEMORY, module.SWAP, module.CPUS) == (4 * 1024**3, 8 * 1024**3, 2.0)
    assert module.parse_bytes('512m') == 512 * 1024**2
    assert module.parse_bytes('1.5Gi') == int(1.5 * 1024**3)

    def runtime(memory, swap, cpus):
        return SimpleNamespace(
            attrs={
                'Config': {'Env': [module.BROWSER_ENV]},
                'HostConfig': {'Memory': memory, 'MemorySwap': memory + swap, 'NanoCpus': int(cpus * 1e9)},
            }
        )

    assert module.runtime_matches_settings(runtime(4 * 1024**3, 8 * 1024**3, 2))
    # A stopped container made with old limits is recreated on its next start (home volume kept).
    assert not module.runtime_matches_settings(runtime(2 * 1024**3, 0, 1))
    with pytest.raises(RuntimeError):
        controller(monkeypatch, SANDBOX_MEMORY='64m')


def test_agent_commands_never_wait_without_bound(monkeypatch):
    module = controller(monkeypatch, SANDBOX_AGENT_WAIT_SECONDS='0.05')
    row = module.get_workspace('u1', 'personal')

    async def enter():
        async with module.agent_execution(row):
            return 'ran'

    async def scenario():
        assert await enter() == 'ran'
        # Manual control answers at once instead of waiting for someone to resume the queue.
        module.manual.add(row['id'])
        with pytest.raises(module.Busy, match='manual control'):
            await enter()
        module.manual.discard(row['id'])
        # A previous command still holding the workspace bounds the wait.
        async with module.locks[row['id']]:
            with pytest.raises(module.Busy, match='still running'):
                await enter()
        assert module.agent_waiters[row['id']] == 0
        # A full controller bounds the queue wait and leaves the workspace stopped, not queued.
        module.active.update(f'other-{i}' for i in range(module.MAX_RUNNING))
        with pytest.raises(module.Busy, match='sandboxes are in use'):
            await module.ensure(row, 0.05)
        assert module.queue == [] and module.get_workspace('u1', 'personal')['status'] == 'stopped'

    asyncio.run(scenario())


def test_long_commands_are_polled_within_the_runtime_wait_limit(monkeypatch):
    module = controller(monkeypatch)
    assert module.TIMEOUT == 1800
    waits = []

    class Response:
        def __init__(self, body):
            self.status, self.body = 200, body

        async def json(self):
            return self.body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *error):
            return False

    class Session:
        def post(self, url, **kwargs):
            return Response({'id': 'p1', 'status': 'running'})

        def get(self, url, params, **kwargs):
            waits.append(float(params['wait']))
            return Response({'id': 'p1', 'status': 'running' if len(waits) < 3 else 'done', 'exit_code': 0})

    row = module.get_workspace('u1', 'personal')
    result, code = asyncio.run(module.run_agent_command(row, Session(), 'http://runtime', {}, {'command': 'x'}))
    assert code == 200 and result['status'] == 'done'
    assert waits == [300, 300, 300]  # open-terminal rejects wait > 300


def test_terminal_takeover_ends_when_the_last_terminal_closes(monkeypatch):
    module = controller(monkeypatch)
    row = module.get_workspace('u1', 'personal')
    identifier = row['id']

    async def scenario():
        # Opening terminals pauses Agent commands only while one is still connected.
        await module.hold_manual_control(row)
        await module.hold_manual_control(row)
        module.drop_manual_control(identifier)
        assert identifier in module.manual
        module.drop_manual_control(identifier)
        assert identifier not in module.manual and module.resume_event(identifier).is_set()
        # An explicit takeover outlives its terminals until the user resumes.
        await module.control('u1', 'personal', module.Control(action='pause'))
        await module.hold_manual_control(row)
        module.drop_manual_control(identifier)
        assert identifier in module.manual
        await module.control('u1', 'personal', module.Control(action='resume'))
        assert identifier not in module.manual and identifier not in module.pinned

    asyncio.run(scenario())

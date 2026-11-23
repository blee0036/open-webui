"""On-demand start and resource limits; no Docker required."""

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
    for key in ('SANDBOX_MEMORY', 'SANDBOX_SWAP', 'SANDBOX_CPUS'):
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

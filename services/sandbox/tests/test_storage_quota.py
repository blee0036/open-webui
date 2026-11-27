"""Sandbox storage soft limit and administrator endpoints; no Docker required."""

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

docker = pytest.importorskip('docker')


def controller(monkeypatch):
    monkeypatch.setenv('SANDBOX_STATE_DIR', tempfile.mkdtemp())
    monkeypatch.setenv('SANDBOX_CONTROLLER_KEY', 'test-key')
    path = Path(__file__).parents[1] / 'controller.py'
    spec = importlib.util.spec_from_file_location('team_test_storage_quota', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def enter(module, row):
    async with module.agent_execution(row):
        return True


def test_agent_is_paused_only_when_measured_storage_exceeds_the_cap(monkeypatch):
    module = controller(monkeypatch)
    personal = module.get_workspace('u1', 'personal')
    project = module.get_workspace('u1', 'p1')
    other = module.get_workspace('u2', 'personal')
    asyncio.run(module.set_limits(module.Limits(limits={'u1': 100, 'u2': None})))

    # Nothing measured yet: never block on a guess.
    assert module.disk_state('u1')['over'] is False

    module.disk_usage.update({personal['id']: 60, project['id']: 50, other['id']: 10**9})
    module.disk_scanned_at = 1
    state = module.disk_state('u1')
    assert (state['used'], state['limit'], state['over']) == (110, 100, True)
    # No cap for u2, however large.
    assert module.disk_state('u2')['over'] is False

    with pytest.raises(module.Busy, match='Sandbox storage is full'):
        asyncio.run(enter(module, personal))
    # Every workspace of the member is paused, since the cap is a total.
    with pytest.raises(module.Busy):
        asyncio.run(enter(module, project))
    assert asyncio.run(enter(module, other)) is True

    module.disk_usage[project['id']] = 0
    assert asyncio.run(enter(module, personal)) is True


def test_limits_are_replaced_as_a_whole(monkeypatch):
    module = controller(monkeypatch)
    asyncio.run(module.set_limits(module.Limits(limits={'u1': 5, 'u2': 7})))
    asyncio.run(module.set_limits(module.Limits(limits={'u2': 9})))
    assert module.disk_state('u1')['limit'] is None
    assert module.disk_state('u2')['limit'] == 9


def test_busy_reaches_the_caller_as_a_readable_error(monkeypatch):
    module = controller(monkeypatch)
    response = asyncio.run(module.busy_handler(None, module.Busy('Sandbox storage is full: clean up')))
    assert response.status_code == 503
    assert b'Sandbox storage is full' in response.body


def test_admin_actions_never_create_workspaces(monkeypatch):
    module = controller(monkeypatch)
    with pytest.raises(module.HTTPException) as missing:
        asyncio.run(module.admin_control('nope', module.Control(action='stop')))
    assert missing.value.status_code == 404
    row = module.get_workspace('u1', 'personal')
    with pytest.raises(module.HTTPException) as bad:
        asyncio.run(module.admin_control(row['id'], module.Control(action='public')))
    assert bad.value.status_code == 400
    with pytest.raises(module.HTTPException) as unconfirmed:
        asyncio.run(module.admin_control(row['id'], module.Control(action='clear')))
    assert unconfirmed.value.status_code == 400
    assert module.state.execute('SELECT COUNT(*) FROM workspace').fetchone()[0] == 1


def test_volume_scan_keeps_only_this_controllers_home_volumes(monkeypatch):
    module = controller(monkeypatch)
    label = module.LABEL
    volumes = [
        {'Name': 'owui-ws-a-home', 'Labels': label, 'UsageData': {'Size': 42}},
        {'Name': 'owui-ws-b-home', 'Labels': {'app': 'other'}, 'UsageData': {'Size': 1}},
        {'Name': 'owui-state', 'Labels': label, 'UsageData': {'Size': 5}},
        {'Name': 'owui-ws-c-home', 'Labels': label, 'UsageData': {'Size': -1}},
    ]

    class Api:
        def df(self):
            return {'Volumes': volumes}

        def close(self):
            pass

    monkeypatch.setattr(module.docker, 'from_env', lambda timeout=30: Api())
    assert module.scan_volumes() == {'a': 42, 'c': 0}


def test_workspace_status_reports_storage(monkeypatch):
    module = controller(monkeypatch)
    row = module.workspace('u1', 'personal')
    asyncio.run(module.set_limits(module.Limits(limits={'u1': 10})))
    module.disk_usage[row['id']] = 4
    module.disk_scanned_at = 123
    disk = asyncio.run(module.status('u1', 'personal'))['disk']
    assert disk == {'used': 4, 'limit': 10, 'over': False, 'scanned_at': 123, 'workspace_used': 4}

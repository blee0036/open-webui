"""Workspace browser toggle; no Docker required (the client is faked)."""

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

docker = pytest.importorskip('docker')


def controller(monkeypatch, enabled):
    monkeypatch.setenv('SANDBOX_STATE_DIR', tempfile.mkdtemp())
    monkeypatch.setenv('SANDBOX_CONTROLLER_KEY', 'test-key')
    if enabled is None:
        monkeypatch.delenv('SANDBOX_ENABLE_BROWSER', raising=False)
    else:
        monkeypatch.setenv('SANDBOX_ENABLE_BROWSER', enabled)
    path = Path(__file__).parents[1] / 'controller.py'
    spec = importlib.util.spec_from_file_location('team_test_controller_' + str(enabled), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def runtime(env, status='exited'):
    removed = []
    return SimpleNamespace(
        status=status,
        attrs={
            'Config': {'Env': env},
            'HostConfig': {'Memory': 2 * 1024**3, 'MemorySwap': 2 * 1024**3, 'NanoCpus': 1_000_000_000},
        },
        remove=lambda: removed.append(True),
        removed=removed,
    )


def test_browser_is_off_unless_enabled(monkeypatch):
    assert controller(monkeypatch, None).BROWSER is False
    assert controller(monkeypatch, 'TRUE').BROWSER is True


def test_existing_container_is_checked_against_the_browser_setting(monkeypatch):
    module = controller(monkeypatch, None)
    assert module.runtime_matches_settings(runtime(['OPEN_TERMINAL_ENABLE_BROWSER=false']))
    # Containers created before the toggle existed have no setting and run with the browser on.
    assert not module.runtime_matches_settings(runtime(['OPEN_TERMINAL_API_KEY=x']))
    assert not module.runtime_matches_settings(runtime(['OPEN_TERMINAL_ENABLE_BROWSER=true']))

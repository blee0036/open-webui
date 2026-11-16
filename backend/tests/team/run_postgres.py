"""Run the same isolated acceptance suite against a disposable PostgreSQL 17 database."""

import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[3]
name = 'owui-team-test-pg-' + uuid.uuid4().hex[:8]
password = secrets.token_hex(24)
database = 'owui_team_test_' + uuid.uuid4().hex[:8]
env = {**os.environ, 'POSTGRES_PASSWORD': password}


def docker(*args):
    return subprocess.run(
        ['docker', *args], check=True, text=True, encoding='utf-8', capture_output=True
    ).stdout.strip()


try:
    subprocess.run(
        [
            'docker',
            'run',
            '-d',
            '--name',
            name,
            '-e',
            'POSTGRES_PASSWORD',
            '-e',
            f'POSTGRES_DB={database}',
            '-p',
            '127.0.0.1::5432',
            'pgvector/pgvector:pg17',
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    for _ in range(60):
        ready = subprocess.run(
            ['docker', 'exec', name, 'pg_isready', '-U', 'postgres', '-d', database], capture_output=True
        )
        if ready.returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError('Test PostgreSQL did not become ready')
    port = json.loads(docker('inspect', name))[0]['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
    env.update(
        TEAM_TEST_DATABASE_URL=f'postgresql://postgres:{password}@127.0.0.1:{port}/{database}',
        PYTHONPATH=str(root / 'backend'),
    )
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'pytest',
            'backend/tests/team',
            '-q',
            '-o',
            'asyncio_default_fixture_loop_scope=function',
            '--disable-warnings',
            '--maxfail=3',
        ],
        cwd=root,
        env=env,
    )
    raise SystemExit(result.returncode)
finally:
    subprocess.run(['docker', 'rm', '-fv', name], capture_output=True)

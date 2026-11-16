"""Isolated databases only; never read the developer's backend/data directory."""

import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest_asyncio

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / '.cache' / ('team-tests-' + uuid.uuid4().hex)
DATA.mkdir(parents=True)
database_url = os.getenv('TEAM_TEST_DATABASE_URL', 'sqlite:///' + (DATA / 'webui.db').as_posix())
if database_url.startswith(('postgres:', 'postgresql:')):
    if not urlsplit(database_url).path.removeprefix('/').startswith('owui_team_test_'):
        raise RuntimeError('PostgreSQL tests require a dedicated owui_team_test_* database')
os.environ.update(
    DATA_DIR=str(DATA),
    DATABASE_URL=database_url,
    USE_SLIM_DOCKER='true',
    WEBUI_SECRET_KEY='isolated-team-test-secret',
    OAUTH_SESSION_TOKEN_ENCRYPTION_KEY='isolated-oauth-test-secret',
    OAUTH_CLIENT_INFO_ENCRYPTION_KEY='isolated-client-test-secret',
    OFFLINE_MODE='true',
    ENABLE_DB_MIGRATIONS='true',
)

from open_webui.config import run_migrations  # noqa: E402,F401
from open_webui.internal.db import Base, get_async_db  # noqa: E402
from open_webui.models.config import Config  # noqa: E402
from open_webui.models import team  # noqa: E402,F401
from sqlalchemy import delete  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def clean_team_tables():
    Config.configure(defaults={}, enable_persistent=True)
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name
        in {
            'quota_entry',
            'quota_audit',
            'quota_period',
            'quota_account',
            'quota_plan',
            'model_price',
            'enterprise_identity',
            'enterprise_login_state',
            'enterprise_connection',
            'config',
            'personal_connection',
            'folder',
            'chat',
            'auth',
            'user',
            'access_grant',
            'tool',
            'file',
            'auth_session',
            'api_key',
        }
    ]
    async with get_async_db() as db:
        for table in reversed(tables):
            await db.execute(delete(table))
        await db.commit()
    yield

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from open_webui.env import OPEN_WEBUI_DIR
from open_webui.internal.db import engine
from sqlalchemy import text


@pytest.mark.asyncio
async def test_upgrade_preserves_legacy_chat_and_folder_semantics():
    def migrate():
        cfg = Config(OPEN_WEBUI_DIR / 'alembic.ini')
        cfg.set_main_option('script_location', str(OPEN_WEBUI_DIR / 'migrations'))
        command.downgrade(cfg, '9f6c2a8d1b43')
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO folder (id,user_id,name,created_at,updated_at,is_expanded) VALUES ('legacy-migrated','owner','Existing folder',1,1,false)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO chat (id,user_id,title,chat,created_at,updated_at,folder_id,archived,pinned) VALUES ('legacy-migrated','owner','Existing chat','{}',1,1,'legacy-migrated',false,false)"
                    )
                )
        finally:
            command.upgrade(cfg, 'head')
        with engine.connect() as conn:
            assert conn.execute(text("SELECT kind FROM folder WHERE id='legacy-migrated'")).scalar() == 'folder'
            assert conn.execute(text("SELECT visibility FROM chat WHERE id='legacy-migrated'")).scalar() == 'private'
            assert (
                conn.execute(text("SELECT folder_id FROM chat WHERE id='legacy-migrated'")).scalar()
                == 'legacy-migrated'
            )

    await asyncio.to_thread(migrate)

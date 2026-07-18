"""測試環境:mock 模式 + 暫存 SQLite,絕不觸碰真實 API。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["MOCK_DATA_MODE"] = "true"
os.environ["DISABLE_SCHEDULER"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///./test_xauusd.db"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _clean_db():
    yield
    db_path = ROOT / "test_xauusd.db"
    if db_path.exists():
        try:
            db_path.unlink()
        except PermissionError:
            pass

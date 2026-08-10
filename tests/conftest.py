"""測試環境:mock 模式 + 暫存 SQLite,絕不觸碰真實 API。"""
import os
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["MOCK_DATA_MODE"] = "true"
os.environ["DISABLE_SCHEDULER"] = "true"
os.environ["APP_ENV"] = "test"          # 認可的測試環境:未設 token 時放行寫入(fail-open 僅限此)
os.environ["DATABASE_URL"] = "sqlite:///./test_xauusd.db"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

# 唯一允許刪除的測試 DB 檔名;任何其他名稱一律拒絕(護欄)。
TEST_DB_NAME = "test_xauusd.db"
# 明確保護的正式/開發 DB 檔名,絕不可被清理邏輯觸碰。
PROTECTED_DB_NAMES = frozenset({"xauusd.db", "xauusd_dev.db"})
_UNLINK_RETRIES = 5
_UNLINK_BACKOFF_SEC = 0.1


def _resolve_test_db_path() -> Path:
    """回傳「已通過安全護欄、確認可刪」的測試 DB 絕對路徑。

    任何一項不成立就 raise(絕不回傳可疑路徑),確保清理邏輯只會刪到測試 DB:
      * DATABASE_URL 必須是 SQLite URL(排除 postgres / 任意 production URL)。
      * URL 必須指向實體檔案(排除 in-memory)。
      * 檔名必須精確等於 test_xauusd.db。
      * 檔名不得是受保護的正式/開發 DB(xauusd.db / xauusd_dev.db)。
      * 解析後的絕對路徑必須落在 repo(ROOT)或當前工作目錄內,不得指向他處。
    """
    from sqlalchemy.engine import make_url

    url = get_settings().database_url
    if not url.startswith("sqlite"):
        raise RuntimeError(
            "拒絕清理測試 DB:DATABASE_URL 不是 SQLite 測試 URL"
            "(可能指向 production/dev DB)。"
        )

    db_file = make_url(url).database
    if not db_file:
        raise RuntimeError("拒絕清理測試 DB:SQLite URL 未含實體檔案路徑(疑似 in-memory)。")

    resolved = Path(db_file).resolve()
    name = resolved.name
    if name in PROTECTED_DB_NAMES:
        raise RuntimeError(f"拒絕清理測試 DB:目標為受保護的 DB 檔名({name})。")
    if name != TEST_DB_NAME:
        raise RuntimeError(f"拒絕清理測試 DB:目標檔名非預期的 {TEST_DB_NAME}({name})。")

    allowed_parents = {ROOT.resolve(), Path.cwd().resolve()}
    if resolved.parent not in allowed_parents:
        raise RuntimeError("拒絕清理測試 DB:測試 DB 路徑不在 repo/測試工作目錄範圍內。")

    return resolved


def _dispose_test_engine() -> None:
    """釋放 SQLite 檔案鎖:dispose 並重置「僅測試用」engine/session module globals。

    Windows 上未關閉的連線會鎖住 DB 檔案使 unlink 失敗;dispose 後檔案鎖才會釋放。
    """
    try:
        from app.db import session as db_session_module

        if db_session_module._engine is not None:
            db_session_module._engine.dispose()
            db_session_module._engine = None
            db_session_module._SessionLocal = None
    except Exception:
        # 找不到 module 或尚未建立 engine 都無妨(session 開始時本就沒有 engine)。
        pass


def _remove_test_db(*, strict: bool) -> None:
    """安全刪除暫存測試 DB。

    strict=True(session 開始):若最終仍無法取得乾淨 DB,主動 raise 讓 pytest 失敗,
      絕不吞掉錯誤後沿用舊 DB。
    strict=False(teardown):OneDrive 同步偶爾短暫鎖檔,重試後仍鎖住則發出不含
      敏感路徑的警告,但不 raise、也不誤刪其他檔案。
    """
    target = _resolve_test_db_path()  # 護欄未過即 raise,不進行任何刪除
    _dispose_test_engine()

    for _ in range(_UNLINK_RETRIES):
        if not target.exists():
            return
        try:
            target.unlink()
            return
        except (PermissionError, OSError):
            time.sleep(_UNLINK_BACKOFF_SEC)

    if not target.exists():
        return

    if strict:
        raise RuntimeError(
            f"測試隔離失敗:session 開始無法刪除舊的 {TEST_DB_NAME}(檔案仍被鎖定)。"
            "為避免沿用上一輪的 llm_usage 等資料造成偽陽性/偽陰性,主動讓測試失敗。"
        )
    warnings.warn(
        f"teardown 未能刪除 {TEST_DB_NAME}(仍被鎖定);已保留,將於下次 session 開始時清除。",
        stacklevel=2,
    )


@pytest.fixture(scope="session", autouse=True)
def _clean_db():
    # 於 session 開始就強制清空:即使上一輪 pytest 程序無法 unlink(OneDrive 同步鎖檔),
    # 新程序啟動時 DB 尚未被連線鎖定,可穩定刪除,確保每次都從乾淨 DB 起跑。
    # strict=True:若仍拿不到乾淨 DB,寧可讓測試失敗,也不沿用舊資料。
    _remove_test_db(strict=True)
    yield
    _remove_test_db(strict=False)

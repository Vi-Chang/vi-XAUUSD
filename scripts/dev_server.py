"""開發用伺服器:強制 mock 模式(不耗真實 API 配額),供本機 UI 開發。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MOCK_DATA_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///./xauusd_dev.db"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8710)

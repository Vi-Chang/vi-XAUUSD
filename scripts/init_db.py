"""資料庫初始化:python scripts/init_db.py(正式環境建議 alembic upgrade head)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import init_db  # noqa: E402

if __name__ == "__main__":
    init_db()
    print("Database initialized.")

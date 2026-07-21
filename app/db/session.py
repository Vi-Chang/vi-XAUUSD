"""SQLAlchemy engine / session。

MVP 使用同步 Session(DB 操作皆為短查詢);docker-compose 用 PostgreSQL,
本機快速展示可用 SQLite(差異記於 ASSUMPTIONS.md)。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().database_url
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def db_session() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """建立所有資料表 + 輕量自動遷移 + 預設帳戶種子。

    (正式環境建議 alembic;此處的 ALTER 僅涵蓋「既有表補新欄位」的簡單情境。)
    """
    from sqlalchemy import inspect, text

    from app.db import models  # noqa: F401  (註冊 metadata)
    engine = get_engine()
    models.Base.metadata.create_all(engine)

    # 輕量遷移:既有 DB 補新欄位(create_all 不會改既有表)
    migrations: dict[str, dict[str, str]] = {
        "positions": {"account_id": "INTEGER"},
        "trade_journal": {"account_id": "INTEGER"},
        "mentor_signals": {   # IMPORT-MENTOR-HISTORY 歷史紀錄擴充
            "status": "VARCHAR(8) DEFAULT 'OPEN'",
            "open_time": "TIMESTAMPTZ", "close_time": "TIMESTAMPTZ",
            "close_price": "FLOAT", "lots": "FLOAT",
            "pl_usd": "FLOAT", "swap_usd": "FLOAT", "net_usd": "FLOAT",
            "points": "FLOAT", "r_multiple": "FLOAT",
            "r_source": "VARCHAR(12)", "import_batch": "VARCHAR(48)",
            "account_no": "VARCHAR(24)",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, cols_ddl in migrations.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col, ddl in cols_ddl.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        # 冪等匯入唯一索引(SQLite 與 PostgreSQL 皆支援 IF NOT EXISTS)
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_mentor_import ON mentor_signals "
            "(account_no, close_time, entry_price, close_price)"))

    # 預設帳戶種子(帳戶A 老師帶單 / 帳戶B 自己交易)
    from datetime import datetime, timezone
    with db_session() as db:
        if db.query(models.Account).count() == 0:
            now = datetime.now(timezone.utc)
            db.add(models.Account(name="帳戶A・老師帶單", strategy_source="TEACHER",
                                  description="跟隨老師訊號執行的交易", created_at=now))
            db.add(models.Account(name="帳戶B・自己交易", strategy_source="SELF",
                                  description="依本系統/自己判斷執行的交易", created_at=now))

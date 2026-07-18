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

    # 輕量遷移:既有 DB 補 account_id 欄位(create_all 不會改既有表)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in ("positions", "trade_journal"):
            cols = {c["name"] for c in inspector.get_columns(table)}
            if "account_id" not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER"))

    # 預設帳戶種子(帳戶A 老師帶單 / 帳戶B 自己交易)
    from datetime import datetime, timezone
    with db_session() as db:
        if db.query(models.Account).count() == 0:
            now = datetime.now(timezone.utc)
            db.add(models.Account(name="帳戶A・老師帶單", strategy_source="TEACHER",
                                  description="跟隨老師訊號執行的交易", created_at=now))
            db.add(models.Account(name="帳戶B・自己交易", strategy_source="SELF",
                                  description="依本系統/自己判斷執行的交易", created_at=now))

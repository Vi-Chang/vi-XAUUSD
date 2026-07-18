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
    """建立所有資料表(開發/SQLite 用;正式環境建議 alembic upgrade head)。"""
    from app.db import models  # noqa: F401  (註冊 metadata)
    models.Base.metadata.create_all(get_engine())

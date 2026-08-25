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

    from app.db import models
    engine = get_engine()
    models.Base.metadata.create_all(engine)

    # 輕量遷移:既有 DB 補新欄位(create_all 不會改既有表)
    migrations: dict[str, dict[str, str]] = {
        "positions": {
            "account_id": "INTEGER",
            "position_timeframe": "VARCHAR(8) DEFAULT 'unknown'",
            "original_thesis": "VARCHAR(500) DEFAULT ''",
            "max_loss_usd": "FLOAT",
            "allow_event_hold": "BOOLEAN",
        },
        "trade_journal": {"account_id": "INTEGER"},
        "directional_alert_states": {
            "last_closed_price": "FLOAT",
            "last_event": "VARCHAR(32) DEFAULT ''",
        },
        "telegram_notifications": {
            "semantic_dedup_key": "VARCHAR(64)",
            "event_key": "VARCHAR(128)",
            "event_type": "VARCHAR(48) DEFAULT ''",
            "symbol": "VARCHAR(32) DEFAULT 'XAUUSD'",
            "state_version": "INTEGER DEFAULT 0",
            "incident_id": "VARCHAR(64) DEFAULT ''",
            "payload_hash": "VARCHAR(64)",
            "sender_worker_id": "VARCHAR(64) DEFAULT ''",
            "decision_id": "VARCHAR(64) DEFAULT ''",
            "decision_version": "INTEGER DEFAULT 0",
            "cancellation_reason": "TEXT DEFAULT ''",
            "decision_snapshot": "JSON DEFAULT '{}'",
        },
        "analysis_runs": {
            "signal_score": "INTEGER",
            "grading_version": "VARCHAR(32) DEFAULT ''",
            "trade_status": "VARCHAR(32) DEFAULT 'WAIT_CONFIRMATION'",
            "can_enter": "BOOLEAN DEFAULT FALSE",
            "blocked_reason": "TEXT DEFAULT ''",
        },
        "decision_events": {
            "scenario_type": "VARCHAR(40) DEFAULT ''",
            "scenario_version": "INTEGER DEFAULT 1",
            "entry_quality_score": "INTEGER",
            "expected_rr": "FLOAT",
            "event_type": "VARCHAR(48) DEFAULT 'DECISION_UPDATED'",
            "event_version": "INTEGER DEFAULT 1",
            "setup_id": "VARCHAR(64) DEFAULT ''",
            "position_id": "VARCHAR(64) DEFAULT ''",
            "snapshot_id": "VARCHAR(64) DEFAULT ''",
            "event_time_utc": "VARCHAR(64) DEFAULT ''",
            "notification_eligible": "BOOLEAN DEFAULT FALSE",
            "notification_reason": "VARCHAR(64) DEFAULT ''",
            "notification_priority": "VARCHAR(16) DEFAULT 'DEBUG'",
        },
        "decision_event_outcomes": {
            "setup_type": "VARCHAR(40) DEFAULT 'OTHER'",
            "market_regime": "VARCHAR(40) DEFAULT 'NO_EDGE'",
            "entry_quality_score": "INTEGER",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, cols_ddl in migrations.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col, ddl in cols_ddl.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        # 已永久移除的老師資料與交易教練資料，不保留歷史表或相容層。
        conn.execute(text("DROP TABLE IF EXISTS mentor_signals"))
        conn.execute(text("DROP TABLE IF EXISTS behavior_flags"))
        conn.execute(text(
            "UPDATE positions SET account_id = NULL WHERE account_id IN "
            "(SELECT id FROM accounts WHERE strategy_source = 'TEACHER')"))
        conn.execute(text("DELETE FROM accounts WHERE strategy_source = 'TEACHER'"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_semantic_dedup "
            "ON telegram_notifications (semantic_dedup_key)"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_event_key "
            "ON telegram_notifications (event_key)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_telegram_payload_dedup "
            "ON telegram_notifications (symbol, event_type, payload_hash, sent_at)"))

    from app.services.confidence_history import backfill_confidence_history
    backfill_confidence_history()

    # 僅保留使用者自己的實際交易帳戶。
    from datetime import datetime, timezone
    with db_session() as db:
        if db.query(models.Account).count() == 0:
            now = datetime.now(timezone.utc)
            db.add(models.Account(name="我的交易帳戶", strategy_source="SELF",
                                  description="依本系統/自己判斷執行的交易", created_at=now))

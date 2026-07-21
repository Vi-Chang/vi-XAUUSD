"""老師帶單歷史匯入:驗算、冪等、進行中/歷史分離、不影響決策。"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db.session import init_db


@pytest.fixture(autouse=True)
def _db():
    init_db()
    from app.db.models import MentorSignal
    from app.db.session import db_session
    with db_session() as db:
        db.query(MentorSignal).delete()
    yield


def _synthetic_file(tmp_path: Path) -> Path:
    """合成 3 筆(2 勝 1 負)符合 points×lots×100=pl 驗算的資料檔。"""
    trades = [
        {"id": "T-1", "close_time": "2026-06-02 10:00:00", "symbol": "XAUUSD",
         "side": "BUY", "lots": 0.1, "entry": 4000.0, "exit": 4010.0, "points": 10.0,
         "pl_usd": 100.0, "swap_usd": 0.5, "net_usd": 99.5, "result": "WIN",
         "source": "MENTOR", "account": "TEST01"},
        {"id": "T-2", "close_time": "2026-06-03 11:00:00", "symbol": "XAUUSD",
         "side": "SELL", "lots": 0.1, "entry": 4020.0, "exit": 4005.0, "points": 15.0,
         "pl_usd": 150.0, "swap_usd": 0.0, "net_usd": 150.0, "result": "WIN",
         "source": "MENTOR", "account": "TEST01"},
        {"id": "T-3", "close_time": "2026-06-04 12:00:00", "symbol": "XAUUSD",
         "side": "BUY", "lots": 0.1, "entry": 4030.0, "exit": 4022.0, "points": -8.0,
         "pl_usd": -80.0, "swap_usd": 0.0, "net_usd": -80.0, "result": "LOSS",
         "source": "MENTOR", "account": "TEST01"},
    ]
    data = {"account": "TEST01", "symbol": "XAUUSD", "source": "MENTOR",
            "contract_size": 100, "timezone": "UTC+8",
            "known_gaps": ["2026-06-10 ~ 2026-06-12"], "trades": trades}
    p = tmp_path / "mentor_test.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _import(path: Path, expected: dict):
    """以合成預期值執行匯入腳本核心(繞過寫死的正式批次對帳值)。"""
    import scripts.import_mentor_history as imp
    old_expected = imp.EXPECTED
    imp.EXPECTED = expected
    old_argv = imp.sys.argv
    imp.sys.argv = ["import", str(path)]
    try:
        imp.main()
    finally:
        imp.EXPECTED = old_expected
        imp.sys.argv = old_argv


SYN_EXPECTED = {"count": 3, "wins": 2, "losses": 1, "net_pl": 170.0,
                "net_after_fees": 169.5, "gross_profit": 250.0,
                "gross_loss": -80.0, "profit_factor": 3.125}


class TestImport:
    def test_import_and_reconcile(self, tmp_path):
        path = _synthetic_file(tmp_path)
        _import(path, SYN_EXPECTED)
        from app.services.mentor_service import history_block
        h = history_block()
        assert h["summary"]["count"] == 3
        assert h["summary"]["wins"] == 2
        assert h["summary"]["net_pl_usd"] == 170.0
        assert h["summary"]["net_after_fees_usd"] == 169.5
        # 缺停損就是 null,不得回填
        assert all(t["stop_loss"] is None for t in h["trades"])
        assert all(t["r_multiple"] is None for t in h["trades"])
        # 時區:UTC+8 10:00 → UTC 02:00
        t1 = [t for t in h["trades"] if "02:00" in t["close_time"]]
        assert t1, h["trades"]

    def test_idempotent_rerun(self, tmp_path):
        path = _synthetic_file(tmp_path)
        _import(path, SYN_EXPECTED)
        _import(path, SYN_EXPECTED)   # 重跑
        from app.services.mentor_service import history_block
        assert history_block()["summary"]["count"] == 3   # 不得重複

    def test_reconcile_mismatch_aborts(self, tmp_path):
        path = _synthetic_file(tmp_path)
        bad = dict(SYN_EXPECTED, count=99)
        with pytest.raises(SystemExit):
            _import(path, bad)
        from app.services.mentor_service import history_block
        assert history_block()["summary"]["count"] == 0   # 一筆都沒寫


class TestSeparation:
    def test_closed_history_excluded_from_active_and_comparison(self, tmp_path):
        from app.services import mentor_service as ms
        _import(_synthetic_file(tmp_path), SYN_EXPECTED)
        # 進行中清單不含歷史單
        assert ms.list_active_signals() == []
        block = ms.comparison_block("PREPARE_LONG", 4000.0)
        assert block["has_signals"] is False
        # 新增一筆進行中訊號 → 兩區各自獨立
        ms.create_signal(direction="LONG", entry_price=4000.0)
        assert len(ms.list_active_signals()) == 1
        assert ms.history_block()["summary"]["count"] == 3

    async def test_history_does_not_affect_analysis(self, tmp_path):
        """鐵律:歷史匯入單不影響決策/證據分數/持倉判斷。"""
        from app.db.models import Position
        from app.db.session import db_session
        from app.providers.mock import MockProvider
        from app.services.analysis_service import run_analysis
        with db_session() as db:
            db.query(Position).delete()
        base = await run_analysis(MockProvider(), trigger="test")
        _import(_synthetic_file(tmp_path), SYN_EXPECTED)
        after = await run_analysis(MockProvider(), trigger="test")
        assert after.decision.action == base.decision.action
        assert after.decision.evidence_score == base.decision.evidence_score
        assert after.position_management.has_position is False
        assert after.mentor_comparison.has_signals is False  # CLOSED 不進比對


class TestHistoryApi:
    def test_endpoint(self, tmp_path):
        from fastapi.testclient import TestClient

        from app.main import app
        _import(_synthetic_file(tmp_path), SYN_EXPECTED)
        with TestClient(app) as c:
            h = c.get("/api/mentor/history").json()
            assert h["summary"]["count"] == 3
            assert h["summary"]["profit_factor"] == 3.125
            assert "known_gaps" in h
            assert "不影響" in h["note"]

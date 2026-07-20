"""老師帶單(mentor_signal)— 純參考,絕不影響持倉判斷/決策/證據分數。"""
import pytest

from app.db.session import init_db
from app.services import mentor_service as ms


@pytest.fixture(autouse=True)
def _db():
    init_db()
    # 清掉殘留老師帶單
    from app.db.models import MentorSignal
    from app.db.session import db_session
    with db_session() as db:
        db.query(MentorSignal).delete()
    yield


def test_create_and_list():
    ms.create_signal(direction="LONG", entry_price=4000.0, stop_loss=3990.0,
                     targets=[4020], note="老師說回踩做多")
    sigs = ms.list_active_signals()
    assert len(sigs) == 1
    assert sigs[0].direction == "LONG"


def test_invalid_direction():
    with pytest.raises(ValueError):
        ms.create_signal(direction="BUY", entry_price=4000.0)


def test_comparison_alignment():
    from app.db.models import MentorSignal
    sig = MentorSignal(direction="LONG", entry_price=4000.0, signal_time=ms._now(),
                       is_active=True, created_at=ms._now())
    # 系統做多 → 一致
    c = ms.compare_signal(sig, "PREPARE_LONG", 4005.0)
    assert c["alignment"] == "ALIGNED"
    assert c["entry_vs_current"] == 5.0
    assert "高 5.0" in c["entry_vs_current_text"]
    # 系統做空 → 相反
    assert ms.compare_signal(sig, "SHORT", 4005.0)["alignment"] == "OPPOSITE"
    # 系統無方向 → 無法比對
    assert ms.compare_signal(sig, "NO_TRADE", 4005.0)["alignment"] == "SYSTEM_NEUTRAL"


def test_deactivate():
    s = ms.create_signal(direction="SHORT", entry_price=4000.0)
    ms.deactivate_signal(s["id"])
    assert ms.list_active_signals() == []


class TestMentorNeverAffectsDecision:
    """核心保證:老師帶單不算持倉、不切 MANAGE、不加減證據分數。"""

    async def test_mentor_signal_does_not_trigger_manage(self, monkeypatch):
        from app.providers.mock import MockProvider
        from app.services import analysis_service
        from app.services.market_calendar import market_is_open

        # 確保無「我的持倉」,只有老師帶單
        from app.db.models import Position
        from app.db.session import db_session
        with db_session() as db:
            db.query(Position).delete()

        base = await analysis_service.run_analysis(MockProvider(), trigger="test")
        # 加入老師帶單後再跑一次
        ms.create_signal(direction="LONG", entry_price=4000.0, stop_loss=3990.0)
        after = await analysis_service.run_analysis(MockProvider(), trigger="test")

        # 有老師帶單但我空手 → 不得變成 MANAGE、has_position 仍 False
        assert after.position_management.has_position is False
        assert after.decision.action != "MANAGE"
        # 決策動作與證據分數不受老師帶單影響(同輸入應相同)
        assert after.decision.action == base.decision.action
        assert after.decision.evidence_score == base.decision.evidence_score
        assert after.bias_analysis.bull_pct == base.bias_analysis.bull_pct
        # 但比對區塊有出現老師帶單
        assert after.mentor_comparison.has_signals is True
        assert len(after.mentor_comparison.signals) == 1


def test_mentor_api_flow():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/mentor/signals", json={
            "direction": "SHORT", "entry_price": 4010.0, "stop_loss": 4020.0,
            "note": "壓力區放空"})
        assert r.status_code == 200
        sid = r.json()["id"]
        block = c.get("/api/mentor/signals").json()
        assert block["has_signals"] is True
        assert "不影響" in block["note"]
        assert c.post(f"/api/mentor/signals/{sid}/deactivate").json()["ok"] is True
        assert c.get("/api/mentor/signals").json()["has_signals"] is False

"""手動持倉管理(spec 十七/十九):R 計算、行為偵測、API 流程。"""
import pytest

from app.db.session import init_db
from app.services import position_service as ps


@pytest.fixture(autouse=True)
def _db():
    init_db()
    yield


def test_create_validates_stop_side():
    with pytest.raises(ValueError):
        ps.create_position(side="LONG", entry_price=4000.0, stop_loss=4010.0, lot_size=0.1)
    with pytest.raises(ValueError):
        ps.create_position(side="SHORT", entry_price=4000.0, stop_loss=3990.0, lot_size=0.1)
    with pytest.raises(ValueError):
        ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0, lot_size=0)


def test_r_multiple_and_pnl():
    pos = ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0, lot_size=0.1)
    assert ps.r_multiple(pos, 4020.0) == 2.0     # 風險 10,獲利 20
    assert ps.r_multiple(pos, 3990.0) == -1.0
    # PnL = 20 × 0.1 手 × 100 oz = 200
    assert ps.unrealized_pnl(pos, 4020.0) == 200.0
    short = ps.create_position(side="SHORT", entry_price=4000.0, stop_loss=4010.0, lot_size=0.1)
    assert ps.r_multiple(short, 3980.0) == 2.0


def test_stop_widening_flagged():
    pos = ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0, lot_size=0.1)
    # 往獲利方向移動:不觸發
    _, flag = ps.modify_stop(pos.id, 3995.0)
    assert flag is None
    # 往虧損方向移動:STOP_WIDENING
    updated, flag = ps.modify_stop(pos.id, 3985.0)
    assert flag == "STOP_WIDENING"
    assert updated.stop_modification_history[-1]["widening"] is True
    flags = ps.recent_behavior_flags()
    assert any(f["flag"] == "STOP_WIDENING" for f in flags)
    # R 分母使用「初始停損」3990,不因移動而漂移
    assert ps.r_multiple(updated, 4020.0) == 2.0


def test_early_exit_flagged_and_full_close():
    pos = ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0, lot_size=0.1)
    # 未達 1R(R=0.5)平掉 80% → EARLY_EXIT
    updated, flag = ps.partial_exit(pos.id, 80, 4005.0)
    assert flag == "EARLY_EXIT"
    assert updated.is_open
    assert ps.remaining_fraction(updated) == pytest.approx(0.2)
    # 平掉剩餘 → 自動關倉
    updated, _ = ps.close_position(pos.id, 4010.0)
    assert not updated.is_open
    assert updated.close_time is not None


def test_recommended_action_stages():
    pos = ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0, lot_size=0.1)
    assert "第一階段" in ps.recommended_action(pos, 4005.0)[0]   # 0.5R
    assert "第二階段" in ps.recommended_action(pos, 4012.0)[0]   # 1.2R
    assert "第三階段" in ps.recommended_action(pos, 4025.0)[0]   # 2.5R
    assert "停損水位" in ps.recommended_action(pos, 3988.0)[0]   # < -1R


def test_positions_api_flow():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/positions", json={
            "side": "LONG", "entry_price": 4000.0, "stop_loss": 3990.0,
            "lot_size": 0.1, "planned_targets": [4020.0, 4040.0]})
        assert r.status_code == 200
        pid = r.json()["id"]
        assert r.json()["remaining_percent"] == 100.0

        assert c.post("/api/positions", json={
            "side": "LONG", "entry_price": 4000.0, "stop_loss": 4010.0,
            "lot_size": 0.1}).status_code == 400

        r = c.post(f"/api/positions/{pid}/stop", json={"stop_loss": 3985.0})
        assert r.json()["behavior_flag"] == "STOP_WIDENING"

        r = c.post(f"/api/positions/{pid}/partial_exit",
                   json={"percent": 30, "price": 4015.0})
        assert r.status_code == 200
        assert r.json()["remaining_percent"] == 70.0

        r = c.post(f"/api/positions/{pid}/close", json={"price": 4020.0})
        assert not r.json()["is_open"]

        flags = c.get("/api/behavior/flags").json()
        assert any(f["flag"] == "STOP_WIDENING" for f in flags)

        rows = c.get("/api/positions").json()
        assert any(p["id"] == pid for p in rows)


def test_analysis_sets_manage_when_position_open():
    import asyncio

    from app.providers.mock import MockProvider
    from app.services.analysis_service import run_analysis
    ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0, lot_size=0.1)
    result = asyncio.run(run_analysis(MockProvider(), trigger="test"))
    assert result.position_management.has_position
    assert result.position_management.recommended_action
    assert result.decision.action in ("MANAGE", "NO_TRADE")  # 休市時仍為 NO_TRADE
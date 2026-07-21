"""P0:以資料源為 key 的 Price Offset + fail-safe(未校準 → NO-SIGNAL)。"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db.session import init_db
from app.services import price_offset as po


@pytest.fixture(autouse=True)
def _db():
    init_db()
    # 清掉所有 offset 設定,每測試乾淨起步
    from app.db.models import SystemSetting
    from app.db.session import db_session
    with db_session() as db:
        db.query(SystemSetting).filter(
            SystemSetting.key.like("price_offset%")).delete(synchronize_session=False)
    po._last_active_source = None
    yield


def _sample_result() -> dict:
    return {
        "current_price": {"bid": 4017.0, "ask": 4017.4, "mid": 4017.2,
                          "provider": "twelve_data"},
        "key_levels": {"strong_support_zones": [{"price_low": 4000.0, "price_high": 4002.0}]},
        "decision": {"action": "PREPARE_LONG", "confidence_grade": "A",
                     "evidence_score": 60, "reason": "x"},
        "long_scenario": {"status": "PREPARE", "entry_zone_id": "SUP_ZONE_01",
                          "stop_loss_id": "SWING_LOW_15M_01",
                          "invalidation_id": "SWING_LOW_15M_01",
                          "target_ids": ["T1"],
                          "risk_reward": [2.0],
                          "resolved_prices": {
            "SUP_ZONE_01": {"price_low": 4019.8, "price_high": 4020.2},
            "SWING_LOW_15M_01": {"price_low": 4015.0, "price_high": 4015.0},
            "T1": {"price_low": 4030.0, "price_high": 4030.0},
        }},
        "short_scenario": {"status": "WATCH", "target_ids": [],
                           "resolved_prices": {}},
    }


class TestPerSourceTable:
    def test_offsets_keyed_by_source(self, monkeypatch):
        po.set_offset(-0.48, source="twelve_data")
        po.set_offset(0.25, source="capital_com")
        td = po.get_offset_for("twelve_data")
        cc = po.get_offset_for("capital_com")
        assert td["value"] == -0.48 and td["calibrated"]
        assert cc["value"] == 0.25 and cc["calibrated"]
        assert td["broker"] == "TMGM"

    def test_legacy_single_value_migrates_to_twelve_data(self):
        from app.db.models import SystemSetting
        from app.db.session import db_session
        with db_session() as db:
            db.add(SystemSetting(key="price_offset", value="-0.3",
                                 updated_at=datetime.now(timezone.utc)))
        info = po.get_offset_for("twelve_data")
        assert info["calibrated"] and info["value"] == -0.3

    def test_label_fields_follow_active_source(self, monkeypatch):
        monkeypatch.setattr(po, "active_source", lambda: "capital_com")
        po.set_offset(0.1, source="capital_com")
        info = po.offset_info()
        assert info["analysis_source"] == "capital_com"   # 動態,非寫死
        assert "capital_com" in info["formula"]


class TestFailSafe:
    def test_uncalibrated_source_blocks_signals(self, monkeypatch):
        """驗收:active_source 無 offset → 停止出訊 + 警示。"""
        monkeypatch.setattr(po, "active_source", lambda: "capital_com")
        out = po.apply_offset_to_result(_sample_result())
        assert out["offset_info"]["calibrated"] is False
        assert out["no_signal"] is True
        sc = out["long_scenario"]
        assert sc["resolved_prices"] == {} and sc["entry_zone_id"] is None
        assert sc["target_ids"] == [] and sc["risk_reward"] == []
        assert out["decision"]["action"] == "WATCH"
        assert out["decision"]["evidence_score"] == 0
        assert "未校準" in out["decision"]["reason"]

    def test_stale_offset_blocks_signals(self, monkeypatch):
        """超過時效(24h)→ 同樣 NO-SIGNAL,不得用舊值出訊。"""
        from app.db.models import SystemSetting
        from app.db.session import db_session
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        with db_session() as db:
            db.add(SystemSetting(key="price_offset:capital_com",
                                 value=json.dumps({"broker": "TMGM", "value": -0.5,
                                                   "updated_at": old}),
                                 updated_at=datetime.now(timezone.utc)))
        monkeypatch.setattr(po, "active_source", lambda: "capital_com")
        info = po.get_offset_for("capital_com")
        assert info["calibrated"] is False and "時效" in info["reason"]
        out = po.apply_offset_to_result(_sample_result())
        assert out["no_signal"] is True

    def test_fresh_calibration_restores_signals(self, monkeypatch):
        monkeypatch.setattr(po, "active_source", lambda: "capital_com")
        po.set_offset(-0.48, source="capital_com")
        out = po.apply_offset_to_result(_sample_result())
        assert "no_signal" not in out
        lp = out["long_scenario"]["resolved_prices"]
        assert lp["SUP_ZONE_01"]["price_low"] == 4019.32     # 4019.8 - 0.48
        assert lp["SWING_LOW_15M_01"]["price_low"] == 4014.52
        assert lp["T1"]["price_low"] == 4029.52
        assert lp["T1"]["offset_applied"] == -0.48

    def test_mock_source_exempt(self, monkeypatch):
        monkeypatch.setattr(po, "active_source", lambda: "mock")
        out = po.apply_offset_to_result(_sample_result())
        assert "no_signal" not in out
        assert out["offset_info"]["calibrated"] is True

    def test_analysis_fields_untouched_by_nosignal(self, monkeypatch):
        monkeypatch.setattr(po, "active_source", lambda: "capital_com")
        out = po.apply_offset_to_result(_sample_result())
        # current_price 與 key_levels 保持分析原值(NO-SIGNAL 只剝劇本價位)
        assert out["current_price"]["mid"] == 4017.2
        assert out["key_levels"]["strong_support_zones"][0]["price_low"] == 4000.0


class TestSourceSwitchLog:
    def test_switch_logged(self, monkeypatch, caplog):
        import logging
        po.set_offset(0.1, source="twelve_data")
        po.set_offset(0.2, source="capital_com")
        monkeypatch.setattr(po, "active_source", lambda: "twelve_data")
        with caplog.at_level(logging.INFO, logger="app.services.price_offset"):
            po.offset_info()
            monkeypatch.setattr(po, "active_source", lambda: "capital_com")
            po.offset_info()
        assert any("OFFSET_SOURCE_SWITCH" in r.message and "capital_com" in r.message
                   for r in caplog.records)


class TestOffsetApi:
    def test_api_roundtrip_active_source(self):
        from fastapi.testclient import TestClient

        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/offset", json={"value": -0.48, "mode": "manual"})
            assert r.status_code == 200
            info = r.json()
            assert info["calibrated"] is True
            assert info["value"] == -0.48
            assert info["analysis_source"]           # 動態來源存在
            assert c.get("/api/offset").json()["value"] == -0.48
            c.post("/api/offset", json={"value": 0.0})

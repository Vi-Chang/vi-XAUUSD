"""P1:空單/多單 SL 方向 — 產生端不變式 + FATAL/REJECT 分級。"""
import logging

import pytest

from app.db.session import init_db
from app.engines.setup_validator import (
    has_fatal, stop_side_ok, validate_prices_detailed,
)


@pytest.fixture(autouse=True)
def _db():
    init_db()
    yield


class TestSeveritySplit:
    def test_wrong_side_sl_is_fatal_and_hides_rr(self):
        """回歸:空單 SL(4066.53) <= Entry(4085.14)→ FATAL;不得輸出 rr1 數值。"""
        detailed = validate_prices_detailed(
            "SHORT", entry=4085.14, sl=4066.52545, tps=[4050.0],
            current_price=4080.0)
        assert has_fatal(detailed)
        msgs = [r["msg"] for r in detailed]
        assert any("SL(4066.53)" in m for m in msgs)          # P3:輸出 2 位小數
        assert any("風報比無法計算" in m for m in msgs)
        assert not any(m.startswith("rr1=") for m in msgs)     # FATAL 時無 rr1 數值

    def test_rr_floor_is_reject_not_fatal(self):
        detailed = validate_prices_detailed(
            "LONG", entry=4000.0, sl=3990.0, tps=[4012.0], current_price=4000.0)
        assert not has_fatal(detailed)
        assert len(detailed) == 1
        assert detailed[0]["severity"] == "REJECT"
        assert "rr1=" in detailed[0]["msg"]

    def test_stop_side_ok(self):
        assert stop_side_ok("LONG", entry=4000.0, sl=3990.0)
        assert not stop_side_ok("LONG", entry=4000.0, sl=4010.0)
        assert stop_side_ok("SHORT", entry=4000.0, sl=4010.0)
        assert not stop_side_ok("SHORT", entry=4000.0, sl=3990.0)


class TestGeneratorInvariant:
    def _decide(self, closes):
        from app.engines.data_quality import DataQualityReport
        from app.engines.key_levels import build_candidate_levels
        from app.engines.market_structure import analyze_structure
        from app.engines.rule_engine import decide
        from tests.helpers import make_df
        df = make_df(closes)
        structures = {"15M": analyze_structure(df, "15M"),
                      "1H": analyze_structure(df, "1H"),
                      "4H": analyze_structure(df, "4H")}
        price = float(df["close"].iloc[-1])
        levels = build_candidate_levels(price=price, atr15=3.0, daily_df=df.iloc[0:0],
                                        structure_reports=structures)
        good = DataQualityReport(status="GOOD", market_open=True)
        return decide(quality=good, structures=structures,
                      indicators_h1={"macd_hist": 1.0},
                      market_state="STRONG_BULL_TREND", price=price, atr15=3.0,
                      levels=levels, event_lockout=False)

    def test_generator_never_emits_fatal(self):
        """產生端修復後:各種行情下都不得再產出 FATAL 級矛盾組合。"""
        from tests.helpers import zigzag_path
        datasets = [
            zigzag_path([(20, 2.0), (8, -1.0), (40, 2.5)]),      # 強漲(原 bug 情境)
            zigzag_path([(20, -2.0), (8, 1.0), (40, -2.5)]),     # 強跌(鏡像)
            zigzag_path([(20, 2.0), (8, -1.0), (20, 2.0), (8, -1.0), (25, 2.0)]),
        ]
        for closes in datasets:
            d = self._decide(closes)
            for sc in (d.long_scenario, d.short_scenario):
                assert not sc.invalid_fatal, \
                    f"產生端仍送出 FATAL:{sc.invalid_reasons}"

    def test_wrong_side_stop_dropped_not_fabricated(self):
        """結構停損在錯誤一側 → 該 setup 無停損、無 rr、非可執行(不硬湊)。"""
        from tests.helpers import zigzag_path
        d = self._decide(zigzag_path([(20, 2.0), (8, -1.0), (40, 2.5)]))
        for sc in (d.long_scenario, d.short_scenario):
            if sc.status == "INVALID":
                continue  # REJECT 級攔截仍允許
            if sc.stop_loss_id is None:
                assert sc.risk_reward == []          # 無停損 → 不得有殘留 rr
                assert sc.status != "PREPARE"        # 不得為可執行方案


class TestRenderBoundaryFatalLog:
    def test_fatal_at_render_logs_error(self, caplog):
        """攔截器接到 FATAL → ERROR log 附完整 setup(代表上游出錯)。"""
        from app.services.freshness import annotate_freshness
        bad = {
            "version": 1, "timestamp_utc": "2026-07-21T00:00:00+00:00",
            "decision": {"action": "PREPARE_SHORT", "confidence_grade": "A",
                         "evidence_score": 50, "reason": "x"},
            "long_scenario": {"status": "WATCH", "target_ids": [], "resolved_prices": {}},
            "short_scenario": {
                "status": "PREPARE", "entry_zone_id": "E", "stop_loss_id": "S",
                "invalidation_id": "S", "target_ids": ["T"],
                "risk_reward": [1.43], "created_at": "2026-07-21T00:00:00+00:00",
                "resolved_prices": {
                    "E": {"price_low": 4085.14, "price_high": 4085.14},
                    "S": {"price_low": 4066.52545, "price_high": 4066.52545},
                    "T": {"price_low": 4050.0, "price_high": 4050.0}}},
        }
        with caplog.at_level(logging.ERROR, logger="app.services.freshness"):
            out = annotate_freshness(bad, current_mid=4080.0)
        sc = out["short_scenario"]
        assert sc["status"] == "INVALID" and sc["invalid_fatal"] is True
        assert sc["resolved_prices"] == {} and sc["risk_reward"] == []
        assert any("SETUP_INVALID_AT_RENDER" in r.message for r in caplog.records)

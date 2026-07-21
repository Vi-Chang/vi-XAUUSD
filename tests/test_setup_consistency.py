"""BUGFIX spec 測試案例 TC-01 ~ TC-13:Setup 價位一致性與時效性。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.session import init_db
from app.engines.setup_validator import validate_prices
from app.services.freshness import annotate_freshness


@pytest.fixture(autouse=True)
def _env():
    init_db()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ═══ Invariant 驗證層(R2)═══

class TestInvariants:
    def test_tc01_long_sl_above_entry_blocked_regression(self):
        """TC-01 + 驗收回歸:本次 bug 實際數據 entry 4037.15 / sl 4058.49 必須被攔截。"""
        reasons = validate_prices("LONG", entry=4037.15, sl=4058.49,
                                  tps=[4075.00, 4080.35, 4085.14],
                                  current_price=4063.64)
        assert reasons, "SL > Entry 的多單必須被攔截"
        assert any("SL" in r and "Entry" in r for r in reasons)

    def test_tc02_short_sl_below_entry_blocked(self):
        reasons = validate_prices("SHORT", entry=4060.0, sl=4050.0,
                                  tps=[4030.0], current_price=4060.0)
        assert any("SL" in r for r in reasons)

    def test_tc03_tp_order_scrambled_blocked(self):
        reasons = validate_prices("LONG", entry=4000.0, sl=3990.0,
                                  tps=[4030.0, 4020.0, 4040.0],  # tp2 < tp1
                                  current_price=4000.0)
        assert any("次序錯亂" in r for r in reasons)

    def test_tc04_rr_below_floor_blocked(self):
        # (tp1-entry)/(entry-sl) = 12/10 = 1.2 < 1.5
        reasons = validate_prices("LONG", entry=4000.0, sl=3990.0,
                                  tps=[4012.0], current_price=4000.0)
        assert any("rr1" in r for r in reasons)

    def test_tc09_hallucinated_price_blocked(self):
        reasons = validate_prices("LONG", entry=3030.0, sl=3020.0,
                                  tps=[3060.0], current_price=4063.0)
        assert any("幻覺" in r for r in reasons)

    def test_valid_setup_passes(self):
        reasons = validate_prices("LONG", entry=4000.0, sl=3990.0,
                                  tps=[4020.0, 4030.0, 4040.0], current_price=4002.0)
        assert reasons == []


# ═══ TC-08:欄位不可單獨更新(frozen)═══

class TestFrozenScenario:
    def test_tc08_field_update_rejected(self):
        from app.schemas.analysis import Scenario
        sc = Scenario(status="PREPARE", entry_zone_id="SUP_ZONE_01")
        with pytest.raises(Exception):   # pydantic frozen → ValidationError
            sc.stop_loss_id = "X"
        # 整組替換是唯一合法途徑
        sc2 = sc.model_copy(update={"stop_loss_id": "SWING_LOW_15M_01"})
        assert sc2.stop_loss_id == "SWING_LOW_15M_01"
        assert sc.stop_loss_id is None   # 原物件不變


# ═══ 規則引擎整合:INVALID 路徑(R2)═══

def _decide_with(closes, price_override=None, atr=3.0):
    from app.engines.data_quality import DataQualityReport
    from app.engines.key_levels import build_candidate_levels
    from app.engines.market_structure import analyze_structure
    from app.engines.rule_engine import decide
    from tests.helpers import make_df
    df = make_df(closes)
    structures = {"15M": analyze_structure(df, "15M"),
                  "1H": analyze_structure(df, "1H"),
                  "4H": analyze_structure(df, "4H")}
    price = price_override if price_override is not None else float(df["close"].iloc[-1])
    levels = build_candidate_levels(price=price, atr15=atr, daily_df=df.iloc[0:0],
                                    structure_reports=structures)
    good = DataQualityReport(status="GOOD", market_open=True)
    return decide(quality=good, structures=structures, indicators_h1={"macd_hist": 1.0},
                  market_state="STRONG_BULL_TREND", price=price, atr15=atr,
                  levels=levels, event_lockout=False), structures


class TestRuleEngineInvalidPath:
    def test_tc01_integration_no_invalid_prices_ever_shown(self):
        """強漲情境(swing low 高於舊支撐)→ 產出的劇本要嘛價位一致、要嘛 INVALID 無價位。"""
        from tests.helpers import zigzag_path
        closes = zigzag_path([(20, 2.0), (8, -1.0), (40, 2.5)])  # 強漲拉開
        d, _ = _decide_with(closes)
        for sc in (d.long_scenario, d.short_scenario):
            if sc.status == "INVALID":
                assert sc.entry_zone_id is None and sc.stop_loss_id is None
                assert sc.target_ids == [] and sc.risk_reward == []
                assert sc.invalid_reasons
            elif sc.entry_zone_id and sc.stop_loss_id:
                # 有完整價位就必須通過不變式(由 validator 保證)
                assert sc.status in ("WATCH", "PREPARE")

    def test_invalid_dominant_downgrades_decision(self, monkeypatch):
        """主方向 setup INVALID → 決策=暫無有效方案、證據分數歸零(TC-01 決策卡部分)。"""
        import app.engines.rule_engine as re_mod
        from app.schemas.analysis import Scenario

        real_build = re_mod._build_scenario

        def broken_build(direction, conditions, **kw):
            sc, rr = real_build(direction, conditions, **kw)
            if direction == "LONG" and conditions:
                return Scenario(status="INVALID",
                                invalid_reasons=["多單 SL(4058.49) >= Entry(4037.15)"]), []
            return sc, rr
        monkeypatch.setattr(re_mod, "_build_scenario", broken_build)

        from tests.helpers import zigzag_path
        closes = zigzag_path([(20, 2.0), (8, -1.0), (20, 2.0), (8, -1.0), (25, 2.0)])
        d, _ = _decide_with(closes)
        assert d.long_scenario.status == "INVALID"
        assert d.evidence_score == 0
        assert d.confidence_grade == "X"
        assert "暫無有效方案" in d.reason


# ═══ TC-05 / TC-06:結構事件掛鉤(R3)═══

class TestStructureEventHook:
    def test_tc05_bos_rebuild_links_event_id(self):
        from tests.helpers import zigzag_path
        closes = zigzag_path([(15, 1.5), (10, -1.8), (15, 1.5), (10, -1.8), (25, 2.5)])
        d, structures = _decide_with(closes)
        ups = [e for e in structures["15M"].events
               if e.still_valid and not e.provisional and e.event_type.endswith("_UP")]
        assert ups, "測試資料應含向上突破"
        latest = ups[-1]
        expected_id = f"15M:{latest.event_type}:{latest.time.isoformat()}"
        assert d.long_scenario.structure_event_id == expected_id

    def test_tc06_choch_down_voids_long_linkage(self):
        """向下反轉後:每次分析全量重建,多單 setup 不得再引用先前向上事件。"""
        from tests.helpers import zigzag_path
        up_closes = zigzag_path([(15, 1.5), (10, -1.8), (25, 2.5)])
        d1, s1 = _decide_with(up_closes)
        old_id = d1.long_scenario.structure_event_id

        down_closes = up_closes + zigzag_path([(6, 1.0), (20, -3.0), (8, 1.2), (20, -3.0)],
                                              start=up_closes[-1])[1:]
        d2, s2 = _decide_with(down_closes)
        downs = [e for e in s2["15M"].events
                 if e.still_valid and not e.provisional and e.event_type.endswith("_DOWN")]
        assert downs, "測試資料應含向下反轉/跌破"
        # 舊多單 setup 已被整組替換;若新多單仍掛事件,不得是反轉前那筆舊向上事件
        assert d2.long_scenario is not d1.long_scenario
        if old_id and d2.long_scenario.structure_event_id == old_id:
            pytest.fail("CHoCH 後多單 setup 仍引用反轉前的舊結構事件")
        assert d2.long_scenario.status != "PREPARE" or \
            d2.short_scenario.status == "PREPARE"


# ═══ TC-07 / TC-13:時效(R4/R6 讀取邊界)═══

def _payload(action="PREPARE_LONG", entry=4037.0, sl=4027.0, tps=(4055.0, 4060.0),
             age_minutes=5.0, rr=(1.8, 2.3)):
    now = datetime.now(timezone.utc)
    ts = now - timedelta(minutes=age_minutes)
    rp = {"E": {"price_low": entry, "price_high": entry},
          "S": {"price_low": sl, "price_high": sl}}
    tp_ids = []
    for i, t in enumerate(tps):
        rp[f"T{i}"] = {"price_low": t, "price_high": t}
        tp_ids.append(f"T{i}")
    sc = {"status": "PREPARE", "entry_zone_id": "E", "stop_loss_id": "S",
          "invalidation_id": "S", "target_ids": tp_ids, "risk_reward": list(rr),
          "resolved_prices": rp, "created_at": ts.isoformat(),
          "snapshot_ts": ts.isoformat(), "invalid_reasons": []}
    return {
        "version": 13, "timestamp_utc": ts.isoformat(),
        "decision": {"action": action, "confidence_grade": "A", "evidence_score": 60,
                     "reason": f"做多條件湊齊,賺賠比最高 {max(rr)} 倍"},
        "long_scenario": sc,
        "short_scenario": {"status": "WATCH", "target_ids": [], "resolved_prices": {},
                           "invalid_reasons": []},
    }


class TestFreshness:
    def test_tc07_price_deviation_marks_stale(self):
        out = annotate_freshness(_payload(entry=4037.0), current_mid=4063.0)  # 偏離 0.64%
        sc = out["long_scenario"]
        assert sc["stale"] is True
        assert "偏離" in sc["stale_reason"]
        assert out["decision"]["action"] == "WATCH"        # 不再是可執行狀態
        assert out["decision"]["evidence_score"] == 0      # 分數不得沿用
        assert "過時" in out["decision"]["reason"]

    def test_fresh_setup_not_stale(self):
        out = annotate_freshness(_payload(entry=4037.0), current_mid=4038.0)  # 0.02%
        assert out["long_scenario"]["stale"] is False
        assert out["decision"]["action"] == "PREPARE_LONG"

    def test_bar_expiry_marks_stale(self):
        out = annotate_freshness(_payload(age_minutes=15 * 9), current_mid=None)
        assert out["long_scenario"]["stale"] is True
        assert "未觸發" in out["long_scenario"]["stale_reason"]

    def test_tc13_snapshot_expired_full_downgrade(self):
        out = annotate_freshness(_payload(age_minutes=31.0), current_mid=4037.5)
        assert out["freshness"]["snapshot_expired"] is True
        assert out["decision"]["action"] == "WATCH"
        assert "過期" in out["decision"]["reason"]

    def test_render_time_invariant_recheck(self):
        """R5:讀取時再驗一次 — 混入 SL>Entry 的舊資料必須在讀取邊界被剝除。"""
        bad = _payload(entry=4037.15, sl=4058.49, tps=(4075.0,))
        out = annotate_freshness(bad, current_mid=4040.0)
        sc = out["long_scenario"]
        assert sc["status"] == "INVALID"
        assert sc["resolved_prices"] == {} and sc["entry_zone_id"] is None
        assert out["decision"]["action"] == "WATCH"
        assert "暫無有效方案" in out["decision"]["reason"]


# ═══ TC-10 / TC-11 / TC-12 ═══

class TestSnapshotCoherence:
    def test_tc10_decision_rr_matches_scenario(self):
        """決策卡文案的賺賠比 = 劇本物件的 risk_reward(同一來源)。"""
        from tests.helpers import zigzag_path
        closes = zigzag_path([(20, 2.0), (8, -1.0), (20, 2.0), (8, -1.0), (25, 2.0)])
        d, _ = _decide_with(closes)
        if d.action.startswith("PREPARE"):
            sc = d.long_scenario if d.action.endswith("LONG") else d.short_scenario
            assert f"{max(sc.risk_reward)}" in d.reason

    async def test_tc11_single_versioned_payload(self):
        """全區塊同版本:decision / mistake / 多週期 / 劇本都在同一份帶版本號的快照內。"""
        from app.providers.mock import MockProvider
        from app.services.analysis_service import run_analysis
        r1 = await run_analysis(MockProvider(), trigger="test")
        r2 = await run_analysis(MockProvider(), trigger="test")
        assert r1.version > 0 and r2.version > r1.version   # 遞增
        payload = r2.model_dump()
        for block in ("decision", "most_likely_user_mistake_now", "timeframes",
                      "long_scenario", "short_scenario"):
            assert block in payload   # 單一物件承載全部區塊 → UI 無從混版本

    def test_tc12_mistake_changes_with_direction(self):
        from app.services.analysis_service import build_mistake
        v13 = build_mistake("STRONG_BULL_TREND", "PREPARE_LONG", [], False)
        v14 = build_mistake("STRUCTURE_TRANSITION", "PREPARE_SHORT", [], False)
        assert v13 != v14
        assert "接多" in v14          # 空方情境:提醒別逆勢接多
        assert "偏多" in v13

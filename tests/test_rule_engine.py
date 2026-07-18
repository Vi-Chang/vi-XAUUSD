"""規則引擎(spec 十四、十五、二十一)與端對端分析管線。"""
from datetime import datetime, timezone

from app.engines.data_quality import DataQualityReport
from app.engines.key_levels import build_candidate_levels
from app.engines.market_structure import analyze_structure
from app.engines.rule_engine import decide, detect_chase
from app.schemas.analysis import AnalysisResult, validate_candidate_refs
from tests.helpers import make_df, zigzag_path


def _setup(closes):
    df = make_df(closes)
    structures = {"15M": analyze_structure(df, "15M"),
                  "1H": analyze_structure(df, "1H"),
                  "4H": analyze_structure(df, "4H")}
    price = float(df["close"].iloc[-1])
    levels = build_candidate_levels(price=price, atr15=3.0, daily_df=df.iloc[0:0],
                                    structure_reports=structures)
    return df, structures, price, levels


def test_bad_data_quality_forces_no_trade():
    bad = DataQualityReport(status="STALE", market_open=True)
    d = decide(quality=bad, structures={}, indicators_h1={}, market_state="RANGE",
               price=4000.0, atr15=3.0, levels=[], event_lockout=False)
    assert d.action == "NO_TRADE"
    assert d.no_trade_code == "NO_TRADE_DATA_QUALITY"
    assert d.confidence_grade == "X"


def test_market_closed_forces_no_trade():
    closed = DataQualityReport(status="GOOD", market_open=False)
    d = decide(quality=closed, structures={}, indicators_h1={}, market_state="RANGE",
               price=4000.0, atr15=3.0, levels=[], event_lockout=False)
    assert d.action == "NO_TRADE"
    assert d.no_trade_code == "NO_TRADE_MARKET_CLOSED"


def test_event_lockout_forces_no_trade():
    good = DataQualityReport(status="GOOD", market_open=True)
    d = decide(quality=good, structures={}, indicators_h1={}, market_state="RANGE",
               price=4000.0, atr15=3.0, levels=[], event_lockout=True)
    assert d.action == "NO_TRADE"
    assert d.no_trade_code == "EVENT_LOCKOUT"


def test_uptrend_produces_watch_or_prepare_long_with_valid_ids():
    closes = zigzag_path([(20, 2.0), (8, -1.0), (20, 2.0), (8, -1.0), (25, 2.0)])
    _, structures, price, levels = _setup(closes)
    good = DataQualityReport(status="GOOD", market_open=True)
    d = decide(quality=good, structures=structures, indicators_h1={"macd_hist": 1.2},
               market_state="STRONG_BULL_TREND", price=price, atr15=3.0,
               levels=levels, event_lockout=False)
    assert d.action in ("WATCH", "PREPARE_LONG")
    assert d.evidence_score > 0
    # 劇本只能引用存在的候選 ID(spec 八)
    known = {lv.level_id for lv in levels}
    result = AnalysisResult(long_scenario=d.long_scenario, short_scenario=d.short_scenario)
    assert validate_candidate_refs(result, known) == []


def test_chase_detected_far_from_structure():
    closes = zigzag_path([(20, 2.0), (8, -1.0), (30, 2.5)])
    _, structures, price, levels = _setup(closes)
    # 價格再extended 20 ATR → 必觸發追多風險
    flags = detect_chase("LONG", price=price + 60.0, atr15=3.0,
                         structures=structures, levels=levels)
    assert any("CHASE_LONG_RISK" in f for f in flags)


async def test_full_pipeline_with_mock_provider():
    """端對端:Mock provider → 完整分析 → 固定 JSON 有效且 ID 引用合法。"""
    from app.db.session import init_db
    from app.providers.mock import MockProvider
    from app.services.analysis_service import run_analysis
    from app.services.market_calendar import market_is_open

    init_db()
    result = await run_analysis(MockProvider(), trigger="test")
    assert result.symbol == "XAUUSD"
    assert result.market_state
    assert result.decision.action in (
        "NO_TRADE", "WATCH", "PREPARE_LONG", "PREPARE_SHORT", "LONG", "SHORT",
        "MANAGE", "EXIT")
    assert result.meta.strategy_version
    assert result.most_likely_user_mistake_now != "" or result.decision.action == "NO_TRADE"
    now = datetime.now(timezone.utc)
    if not market_is_open(now):
        # 休市:必須 NO_TRADE 且不誤報 STALE(spec 四)
        assert result.decision.action == "NO_TRADE"
        assert result.data_quality.status != "STALE"
    # 劇本 resolved_prices 只含合法 ID
    for sc in (result.long_scenario, result.short_scenario):
        for lv_id in sc.resolved_prices:
            assert lv_id in {*sc.target_ids, sc.entry_zone_id, sc.stop_loss_id,
                             sc.invalidation_id}

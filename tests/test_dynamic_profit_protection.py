import pandas as pd

from app.engines.dynamic_profit_protection import evaluate_dynamic_profit


def bars(closes, *, upper_wick=False):
    rows = []
    for close in closes:
        rows.append({"open": close - .8, "high": close + (2 if upper_wick else .5),
                     "low": close - 1, "close": close, "volume": 100})
    return pd.DataFrame(rows, index=pd.date_range("2026-08-24 12:00", periods=len(rows), freq="15min", tz="UTC"))


def payload(price, positions):
    return {"timestamp_utc": "2026-08-24T14:00:00+00:00",
            "normalized_analysis": {"currentPrice": price, "atr15": 2,
                                    "lastClosedCandleTimestamp": "2026-08-24T13:45:00Z"},
            "indicator_snapshot": {"15M": {"stoch_k": 99}},
            "position_management": {"has_position": True, "positions": positions}}


def plans(items):
    mapped = {}
    for item in items:
        pid, entry, pclass = item
        mapped[pid] = {"tradePlanId": pid, "referenceEntry": entry, "direction": "LONG",
                       "tp1Price": 110, "tp2Price": 115, "tp3Price": 120,
                       "initialStop": 95, "strategyStop": 98, "trailingStopPrice": 100,
                       "positionClass": pclass}
    return {"plans": mapped, "activePlans": list(mapped.values())}


def position(pid="p1", entry=100, size=.01, pclass="CORE"):
    return {"position_id": pid, "side": "LONG", "entry_price": entry,
            "position_size": size, "position_class": pclass, "trade_plan_id": pid}


def test_case_a_f_h_tp_extreme_rapid_extension_beats_bullish_macd_for_single_unit():
    frame = bars([100, 102, 105, 107, 109, 109.8], upper_wick=True)
    state, _ = evaluate_dynamic_profit(data=payload(110, [position()]), frame=frame,
        trade_plans=plans([("p1", 100, "CORE")]), break_state={}, previous={})
    result = state["positions"][0]
    assert result["take_profit_priority"] == "HIGH"
    assert result["single_unit_position"] is True
    assert result["position_action"] == "TAKE_PROFIT"


def test_case_b_closed_break_follow_and_hold_allows_extension():
    frame = bars([104, 106, 108, 110.5, 111, 112])
    state, _ = evaluate_dynamic_profit(data=payload(112, [position()]), frame=frame,
        trade_plans=plans([("p1", 100, "CORE")]), break_state={}, previous={})
    result = state["positions"][0]
    assert result["extension_confirmed"] is True
    assert result["position_action"] == "LET_PROFIT_RUN"


def test_case_c_positions_are_managed_independently():
    positions = [position("core", 100, .1, "CORE"), position("addon", 109, .1, "ADD_ON")]
    state, _ = evaluate_dynamic_profit(data=payload(110, positions), frame=bars([104,106,108,109,109.5,109.8]),
        trade_plans=plans([("core",100,"CORE"), ("addon",109,"ADD_ON")]), break_state={}, previous={})
    assert len(state["positions"]) == 2
    assert {x["position_class"] for x in state["positions"]} == {"CORE", "ADD_ON"}
    assert state["positions"][0]["max_allowed_giveback"] != state["positions"][1]["max_allowed_giveback"]


def test_case_d_peak_profit_and_giveback_ratio_are_persisted():
    first, _ = evaluate_dynamic_profit(data=payload(140, [position(size=.1)]), frame=bars([130,132,134,136,138,140]),
        trade_plans=plans([("p1",100,"CORE")]), break_state={}, previous={})
    second, _ = evaluate_dynamic_profit(data=payload(130, [position(size=.1)]), frame=bars([140,138,136,134,132,130]),
        trade_plans=plans([("p1",100,"CORE")]), break_state={}, previous=first)
    result = second["positions"][0]
    assert result["max_unrealized_profit"] == 40
    assert result["current_unrealized_profit"] == 30
    assert result["profit_giveback_ratio"] == .25


def test_case_e_whipsaw_has_tighter_giveback_than_trend():
    kwargs = dict(data=payload(110, [position(size=.1)]), frame=bars([104,106,108,109,109.5,109.8]),
                  trade_plans=plans([("p1",100,"CORE")]), previous={})
    trend, _ = evaluate_dynamic_profit(**kwargs, break_state={"market_regime":"NORMAL"})
    whip, _ = evaluate_dynamic_profit(**kwargs, break_state={"market_regime":"WHIPSAW"})
    assert whip["positions"][0]["max_allowed_giveback"] < trend["positions"][0]["max_allowed_giveback"]


def test_case_g_take_profit_creates_new_reentry_requirement_not_revived_position():
    state, _ = evaluate_dynamic_profit(data=payload(110, [position()]), frame=bars([100,102,105,107,109,109.8]),
        trade_plans=plans([("p1",100,"CORE")]), break_state={}, previous={})
    result = state["positions"][0]
    assert result["reentry_setup_required"] is True
    assert "新的 setup_id" in result["reentry_rule"]


def test_trailing_profit_protection_never_moves_backward():
    first, _ = evaluate_dynamic_profit(data=payload(115, [position(size=.1)]), frame=bars([105,107,109,111,113,115]),
        trade_plans=plans([("p1",100,"CORE")]), break_state={}, previous={})
    old = first["positions"][0]["profit_protection_level"]
    second, _ = evaluate_dynamic_profit(data=payload(108, [position(size=.1)]), frame=bars([115,113,112,110,109,108]),
        trade_plans=plans([("p1",100,"CORE")]), break_state={}, previous=first)
    assert second["positions"][0]["profit_protection_level"] >= old


def test_hard_risk_stop_always_exits_even_if_fast_reclaim_analysis_exists():
    state, _ = evaluate_dynamic_profit(data=payload(94, [position(size=.1)]), frame=bars([100,98,96,94,95,96]),
        trade_plans=plans([("p1",100,"CORE")]),
        break_state={"state": "FAILED_BREAKDOWN"}, previous={})
    result = state["positions"][0]
    assert result["hard_risk_stop_triggered"] is True
    assert result["position_action"] == "EXIT_NOW"
    assert result["hard_risk_stop"] != result["structural_exit_confirmation"]

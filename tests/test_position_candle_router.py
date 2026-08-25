from datetime import datetime, timedelta, timezone

from app.engines.canonical_decision import build_canonical_decision
from app.engines.decision_presentation import format_decision_message
from app.providers.base import Candle
from app.services.closed_candle_service import ClosedCandleService
from tests.test_canonical_decision import payload


def candle(open_hour, open_minute, *, closed=True, close=4668.0):
    opened = datetime(2026, 8, 24, open_hour, open_minute, tzinfo=timezone.utc)
    return Candle("XAUUSD", "15M", opened,
                  opened + timedelta(minutes=15),
                  close - 1, close + 1, close - 2, close,
                  is_closed=closed, data_provider="fixture")


def test_2255_taipei_maps_to_2230_2245_closed_not_forming():
    decision = datetime(2026, 8, 24, 14, 55, tzinfo=timezone.utc)
    rows = [candle(14, 30, close=4668.66), candle(14, 45, closed=False, close=4669)]
    result = ClosedCandleService.latest(rows, timeframe="15M", decision_time=decision)
    assert result.available and result.open_time == "2026-08-24T14:30:00+00:00"
    assert result.close_time == "2026-08-24T14:45:00+00:00"
    assert result.close_price == 4668.66


def test_future_or_forming_candle_never_becomes_closed_confirmation():
    decision = datetime(2026, 8, 24, 14, 40, tzinfo=timezone.utc)
    result = ClosedCandleService.latest(
        [candle(14, 30, closed=True)], timeframe="15M", decision_time=decision)
    assert not result.available and result.error_reason == "DATA_GAP"


def test_position_known_routes_primary_telegram_to_management():
    data = payload(price=4668.66)
    data["position_management"] = {
        "has_position": True, "position_side": "LONG", "entry_price": 4663.38,
        "position_size": .01, "recommended_action": "HOLD",
        "positions": [{"position_id": "P-1", "side": "LONG",
                       "entry_price": 4663.38, "position_size": .01,
                       "position_class": "CORE", "stop_loss": 4659.8}],
    }
    data["closed_candles"] = {"15M": {
        "timeframe": "15M", "open_time": "2026-08-24T14:30:00+00:00",
        "close_time": "2026-08-24T14:45:00+00:00", "close_price": 4667.0,
        "is_closed": True, "available": True, "error_reason": None}}
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["notificationRoute"] == "POSITION_MANAGEMENT"
    assert canonical["newEntryDecision"]["action"] == "WAIT"
    assert canonical["positionManagement"]["tacticalDefense"] == 4659.8
    message = format_decision_message({"event_type": "CANDLE_FINALIZED",
        "currentPrice": 4668.66, "canonicalDecision": canonical})
    assert "【XAUUSD 持倉管理】" in message
    assert "成本 4663.38" in message and "目前動作：續抱" in message
    assert "新開部位：等待" in message and "現在先不要進場" not in message
    assert "22:30–22:45" in message

    behavior_message = format_decision_message({
        "event_type": "MARKET_BEHAVIOR_CHANGED", "marketBehavior": "PULLBACK",
        "currentPrice": 4668.66, "canonicalDecision": canonical})
    assert "【XAUUSD 持倉管理】" in behavior_message
    assert "價格行為改變" not in behavior_message


def test_data_gap_blocks_entry_but_keeps_position_risk_output():
    data = payload(price=4668.66)
    data["position_management"] = {
        "has_position": True, "position_side": "LONG", "entry_price": 4663.38,
        "position_size": .01, "recommended_action": "HOLD",
        "positions": [{"position_id": "P-1", "side": "LONG",
                       "entry_price": 4663.38, "position_size": .01,
                       "stop_loss": 4659.8}],
    }
    data["closed_candles"] = {"15M": {"timeframe": "15M", "available": False,
        "is_closed": False, "error_reason": "DATA_GAP", "open_time": None,
        "close_time": None, "close_price": None}}
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["newEntryDecision"]["tradeStatus"] == "WAIT_DATA_CONFIRMATION"
    assert not canonical["newEntryDecision"]["canEnter"]
    message = format_decision_message({"event_type": "CANDLE_FINALIZED",
        "currentPrice": 4668.66, "canonicalDecision": canonical})
    assert "已收15M資料缺口：已收K線資料缺口" in message
    assert "短線持倉防守：4659.8" in message


def test_multiple_positions_keep_actual_entries_and_independent_actions():
    data = payload(price=4668.66)
    data["position_management"] = {"has_position": True, "position_side": "LONG",
        "entry_price": 4637.27, "position_size": .02, "recommended_action": "HOLD",
        "positions": [
            {"position_id": "CORE", "side": "LONG", "entry_price": 4637.27,
             "position_size": .01, "position_class": "CORE", "stop_loss": 4628},
            {"position_id": "BO", "side": "LONG", "entry_price": 4658.98,
             "position_size": .01, "position_class": "BREAKOUT", "stop_loss": 4655},
        ]}
    data["dynamic_profit_protection"] = {"positions": [
        {"position_id": "CORE", "position_action": "HOLD", "profit_protection_level": 4650},
        {"position_id": "BO", "position_action": "EXIT_NOW", "hard_risk_stop": 4655},
    ]}
    canonical = build_canonical_decision(data, data["final_decision_state"])
    rows = canonical["positionManagement"]["perPositionDecisions"]
    assert [(row["actualEntryPrice"], row["positionAction"]) for row in rows] == [
        (4637.27, "HOLD"), (4658.98, "EXIT_NOW")]

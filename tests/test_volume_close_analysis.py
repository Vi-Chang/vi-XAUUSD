from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import func, select

from app.db.models import TelegramNotification
from app.db.session import db_session, init_db
from app.engines.candle_close_analysis import evaluate_candle_close_reports
from app.engines.decision_presentation import format_decision_message
from app.engines.volume_intelligence import (
    evaluate_volume_intelligence,
    relative_volume_engine,
    volume_proverb,
)
from app.services.decision_outbox import persist_decision_events
from app.services.notification_policy import canonical_dedupe_key, eligibility


def frame(last: dict, *, count: int = 30, base: float = 100.0,
          start: datetime | None = None, minutes: int = 15) -> pd.DataFrame:
    start = start or datetime(2026, 8, 25, tzinfo=timezone.utc)
    rows = []
    for index in range(count-1):
        close = base + index*.1
        rows.append({"open": close-.2, "high": close+.4, "low": close-.5,
                     "close": close, "volume": 100.0, "is_closed": True})
    rows.append({**last, "is_closed": True})
    return pd.DataFrame(rows, index=pd.date_range(
        start, periods=count, freq=f"{minutes}min", tz="UTC"))


def test_case_a_high_volume_bullish_breakout_confirms_long():
    bars = frame({"open": 101, "high": 105, "low": 100.8,
                  "close": 104.8, "volume": 220})
    result = evaluate_volume_intelligence(
        m15_closed=bars, h1_closed=bars, atr15=3, atr1h=3,
        structural_bias="BULLISH")
    m15 = result["timeframes"]["15M"]
    assert m15["volumePriceState"] == "BULLISH_BREAKOUT_VOLUME"
    assert result["volumeScore"]["LONG"] > 0


def test_case_b_high_volume_upper_wick_is_not_strong_bullish():
    bars = frame({"open": 102, "high": 112, "low": 101.5,
                  "close": 103, "volume": 260})
    result = evaluate_volume_intelligence(
        m15_closed=bars, h1_closed=bars, atr15=5, atr1h=5)
    m15 = result["timeframes"]["15M"]
    assert m15["volumePriceState"] == "BUYING_ABSORPTION"
    assert m15["candleAnatomy"]["candleType"] == "REJECTION_UP"


def test_case_c_low_volume_rise_downgrades_but_never_creates_short_entry():
    bars = frame({"open": 102, "high": 104, "low": 101.8,
                  "close": 103.5, "volume": 30})
    result = evaluate_volume_intelligence(
        m15_closed=bars, h1_closed=bars, atr15=3, atr1h=3)
    m15 = result["timeframes"]["15M"]
    assert m15["volumePriceState"] == "LOW_VOLUME_RISE"
    assert m15["longScoreImpact"] < 0
    assert m15["shortScoreImpact"] == 0


def test_case_d_low_volume_drop_does_not_create_long_entry():
    bars = frame({"open": 103, "high": 103.2, "low": 101,
                  "close": 101.5, "volume": 30})
    result = evaluate_volume_intelligence(
        m15_closed=bars, h1_closed=bars, atr15=3, atr1h=3)
    m15 = result["timeframes"]["15M"]
    assert m15["volumePriceState"] == "LOW_VOLUME_DROP"
    assert m15["longScoreImpact"] == 0


def test_case_e_low_volume_pullback_long():
    bars = frame({"open": 103, "high": 103.1, "low": 101.9,
                  "close": 102.4, "volume": 30})
    result = evaluate_volume_intelligence(
        m15_closed=bars, h1_closed=bars, atr15=3, atr1h=3,
        structural_bias="BULLISH")
    assert result["timeframes"]["15M"]["volumePriceState"] == (
        "LOW_VOLUME_PULLBACK_LONG")


def test_case_f_high_volume_false_break_reclaim_is_selling_absorption():
    bars = frame({"open": 103, "high": 104, "low": 95,
                  "close": 103.5, "volume": 260})
    result = evaluate_volume_intelligence(
        m15_closed=bars, h1_closed=bars, atr15=5, atr1h=5)
    assert result["timeframes"]["15M"]["volumePriceState"] == "SELLING_ABSORPTION"


def test_volume_proverbs_are_conditional_and_never_entry_permission():
    expected = {
        "VOLUME_CONFIRMED_DROP": "放量下跌",
        "VOLUME_CONFIRMED_RISE": "放量上漲",
        "LOW_VOLUME_RISE": "縮量上漲",
        "LOW_VOLUME_DROP": "縮量下跌",
        "BUYING_ABSORPTION": "放量不漲",
        "LOW_VOLUME_CONSOLIDATION": "縮量不跌",
    }
    for state, wording in expected.items():
        proverb = volume_proverb({"volumePriceState": state})
        assert wording in proverb["text"]
        assert "必然" not in proverb["text"]
        assert proverb["actionable"] is False
        assert proverb["requiresPriceConfirmation"] is True


def test_volume_payload_has_separate_15m_and_1h_proverbs():
    m15 = frame({"open": 103, "high": 103.2, "low": 101,
                 "close": 101.5, "volume": 30})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 220}, minutes=60)
    result = evaluate_volume_intelligence(
        m15_closed=m15, h1_closed=h1, atr15=3, atr1h=6,
        structural_bias="BULLISH")
    for timeframe in ("15M", "1H"):
        proverb = result["timeframes"][timeframe]["volumeProverb"]
        assert proverb["text"]
        assert proverb["actionable"] is False


def close_context(close15: str, close1h: str, *, blackout: bool = False) -> dict:
    return {
        "symbol": "XAUUSD", "version": 7,
        "timestamp_utc": "2026-08-25T12:00:05+00:00",
        "normalized_analysis": {
            "currentPrice": 104.8, "marketDataStatus": "GOOD",
            "confirmationLevels": [{"kind": "support", "price": 102.0},
                                   {"kind": "resistance", "price": 105.0}],
        },
        "closed_candles": {
            "15M": {"available": True, "close_time": close15, "source": "test"},
            "1H": {"available": True, "close_time": close1h, "source": "test"},
        },
        "event_risk": {"event_lockout": blackout},
    }


def decision(side: str = "LONG") -> dict:
    market = "BULLISH" if side == "LONG" else "BEARISH"
    return {
        "decisionId": "DEC-7", "decisionVersion": 7, "state": "WAIT",
        "finalAction": "WAIT", "canEnter": False, "marketBias": market,
        "structuralBias": market, "liveBiasState": "ALIGNED",
        "executionBias": side,
        "dataHealth": "HEALTHY", "entryConfirmation": "WAIT_CONFIRMATION",
        "entryOpportunityGate": {"entryState": "WATCH", "longScore": 71,
                                 "shortScore": 34, "selected": {}},
        "canonicalDecision": {"marketBias": market, "executionBias": side,
                              "canonicalNextTrigger": {"label": "等待收盤確認"}},
    }


def volume_payload(m15: pd.DataFrame, h1: pd.DataFrame) -> dict:
    return evaluate_volume_intelligence(
        m15_closed=m15, h1_closed=h1, atr15=3, atr1h=6,
        structural_bias="BULLISH")


def test_case_g_new_15m_close_always_creates_one_report():
    m15 = frame({"open": 103, "high": 105, "low": 102.5,
                 "close": 104.8, "volume": 150})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 500}, minutes=60)
    data = close_context("2026-08-25T11:45:00+00:00",
                         "2026-08-25T11:00:00+00:00")
    state, events = evaluate_candle_close_reports(
        data, decision(), volume=volume_payload(m15, h1),
        m15_closed=m15, h1_closed=h1, evaluated_at=data["timestamp_utc"])
    assert len(events) == 1
    assert events[0]["event_type"] == "CANDLE_CLOSE_ANALYSIS_15M"
    assert eligibility(events[0])["eligible"] is True
    assert state["last15mReportTime"] == "2026-08-25T11:45:00+00:00"


def test_case_h_same_15m_worker_trigger_is_deduped():
    m15 = frame({"open": 103, "high": 105, "low": 102.5,
                 "close": 104.8, "volume": 150})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 500}, minutes=60)
    data = close_context("2026-08-25T11:45:00+00:00",
                         "2026-08-25T11:00:00+00:00")
    first, events = evaluate_candle_close_reports(
        data, decision(), volume=volume_payload(m15, h1),
        m15_closed=m15, h1_closed=h1, evaluated_at=data["timestamp_utc"])
    second, repeated = evaluate_candle_close_reports(
        data, decision(), volume=volume_payload(m15, h1),
        m15_closed=m15, h1_closed=h1, previous=first,
        evaluated_at="2026-08-25T12:01:00+00:00")
    assert len(events) == 1 and repeated == []
    assert canonical_dedupe_key(events[0]) == events[0]["eventKey"]
    assert second["last15mReportTime"] == first["last15mReportTime"]


def test_case_h_atomic_outbox_allows_one_row_for_same_close_report():
    init_db()
    m15 = frame({"open": 103, "high": 105, "low": 102.5,
                 "close": 104.8, "volume": 150})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 500}, minutes=60)
    data = close_context("2026-08-25T11:45:00+00:00",
                         "2026-08-25T11:00:00+00:00")
    _, events = evaluate_candle_close_reports(
        {**data, "symbol": "XAUUSD-CLOSE-ATOMIC"}, decision(),
        volume=volume_payload(m15, h1), m15_closed=m15, h1_closed=h1,
        evaluated_at=data["timestamp_utc"])
    assert len(persist_decision_events("XAUUSD-CLOSE-ATOMIC", events)) == 1
    repeated = [{**events[0], "eventId": "CCR-concurrent-worker"}]
    # A late worker may merge its audit fact into the still-pending canonical
    # row, but the database-enforced outbox identity remains exactly one row.
    persist_decision_events("XAUUSD-CLOSE-ATOMIC", repeated)
    with db_session() as db:
        count = db.scalar(select(func.count()).select_from(
            TelegramNotification).where(
                TelegramNotification.symbol == "XAUUSD-CLOSE-ATOMIC"))
    assert count == 1


def test_case_i_hourly_15m_and_1h_are_combined():
    m15 = frame({"open": 103, "high": 105, "low": 102.5,
                 "close": 104.8, "volume": 150})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 500}, minutes=60)
    close = "2026-08-25T12:00:00+00:00"
    data = close_context(close, close)
    state, events = evaluate_candle_close_reports(
        data, decision(), volume=volume_payload(m15, h1),
        m15_closed=m15, h1_closed=h1, evaluated_at=data["timestamp_utc"])
    assert len(events) == 1
    assert events[0]["event_type"] == "CANDLE_CLOSE_ANALYSIS_COMBINED"
    assert state["last15mReportTime"] == state["last1hReportTime"] == close
    message = format_decision_message(events[0])
    assert "15M＋1H 收盤總分析" in message
    assert "15M 口訣：" in message
    assert "1H 口訣：" in message
    assert "必然" not in message


def test_15m_close_report_displays_only_its_own_volume_proverb():
    m15 = frame({"open": 103, "high": 105, "low": 102.5,
                 "close": 104.8, "volume": 150})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 500}, minutes=60)
    data = close_context("2026-08-25T11:45:00+00:00",
                         "2026-08-25T11:00:00+00:00")
    _, events = evaluate_candle_close_reports(
        data, decision(), volume=volume_payload(m15, h1),
        m15_closed=m15, h1_closed=h1, evaluated_at=data["timestamp_utc"])
    message = format_decision_message(events[0])
    assert "15M 口訣：" in message
    assert "1H 口訣：" not in message


def test_case_j_1h_direction_change_is_visible_and_canonical():
    m15 = frame({"open": 103, "high": 105, "low": 102.5,
                 "close": 104.8, "volume": 150})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 500}, minutes=60)
    close = "2026-08-25T12:00:00+00:00"
    data = close_context("2026-08-25T11:45:00+00:00", close)
    previous = {"last15mReportTime": "2026-08-25T11:45:00+00:00",
                "last1hReportTime": "2026-08-25T11:00:00+00:00",
                "lastReport": {"executionBias": "SHORT"}}
    state, events = evaluate_candle_close_reports(
        data, decision("LONG"), volume=volume_payload(m15, h1),
        m15_closed=m15, h1_closed=h1, previous=previous,
        evaluated_at=data["timestamp_utc"])
    assert events[0]["candleCloseReport"]["executionBias"] == "LONG"
    assert any("偏空" in item and "偏多" in item
               for item in events[0]["candleCloseReport"]["whatChanged"])
    assert state["last1hReportTime"] == close


def test_case_k_event_blackout_still_reports_but_blocks_entry():
    m15 = frame({"open": 103, "high": 105, "low": 102.5,
                 "close": 104.8, "volume": 150})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 500}, minutes=60)
    data = close_context("2026-08-25T11:45:00+00:00",
                         "2026-08-25T11:00:00+00:00", blackout=True)
    _, events = evaluate_candle_close_reports(
        data, {**decision(), "canEnter": True, "finalAction": "ENTER_LONG"},
        volume=volume_payload(m15, h1), m15_closed=m15, h1_closed=h1,
        evaluated_at=data["timestamp_utc"])
    report = events[0]["candleCloseReport"]
    assert report["currentAction"] == "BLOCKED_EVENT"
    assert report["canEnter"] is False


def test_case_l_no_finalized_candle_means_no_fake_report():
    m15 = frame({"open": 103, "high": 105, "low": 102.5,
                 "close": 104.8, "volume": 150})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 500}, minutes=60)
    data = close_context("", "")
    data["closed_candles"]["15M"]["available"] = False
    data["closed_candles"]["1H"]["available"] = False
    _, events = evaluate_candle_close_reports(
        data, decision(), volume=volume_payload(m15, h1),
        m15_closed=m15, h1_closed=h1, evaluated_at=data["timestamp_utc"])
    assert events == []


def test_session_normalization_uses_same_session_when_available():
    bars = frame({"open": 102, "high": 104, "low": 101.8,
                  "close": 103.5, "volume": 100})
    result = relative_volume_engine(bars, timeframe="15M", atr=3)
    assert result["session"] in {"ASIA", "EUROPE", "EUROPE_US_OVERLAP", "US"}
    assert result["sessionNormalizedRatio"] is not None


def test_close_report_surfaces_same_cycle_critical_exit_action():
    m15 = frame({"open": 103, "high": 105, "low": 102.5,
                 "close": 104.8, "volume": 150})
    h1 = frame({"open": 101, "high": 105, "low": 100,
                "close": 104, "volume": 500}, minutes=60)
    data = close_context("2026-08-25T11:45:00+00:00",
                         "2026-08-25T11:00:00+00:00")
    _, events = evaluate_candle_close_reports(
        data, decision(), volume=volume_payload(m15, h1),
        m15_closed=m15, h1_closed=h1, evaluated_at=data["timestamp_utc"])
    events[0]["signalFacts"] = [{"event_type": "STOP_TRIGGERED"}]
    message = format_decision_message(events[0])
    assert "防守／退出條件已觸發" in message
    assert "現在能不能進" in message

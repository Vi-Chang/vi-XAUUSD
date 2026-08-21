from datetime import datetime, timedelta, timezone

import pandas as pd

from app.engines.entry_engine import (
    EntryPlan,
    evaluate_entry_engine,
    ordered_profit_targets,
    validate_executable_plan,
)

NOW = datetime(2026, 8, 19, 1, tzinfo=timezone.utc)


def frame(previous, current):
    return pd.DataFrame(
        [
            {
                "open": previous[0],
                "high": previous[1],
                "low": previous[2],
                "close": previous[3],
                "is_closed": True,
            },
            {
                "open": current[0],
                "high": current[1],
                "low": current[2],
                "close": current[3],
                "is_closed": True,
            },
        ]
    )


def scenario(direction, *, tp1=None, tp2=None):
    if direction == "SHORT":
        tp1, tp2 = tp1 or 97, tp2 or 95
    else:
        tp1, tp2 = tp1 or 103, tp2 or 105
    return {
        "target_ids": ["T1", "T2"],
        "resolved_prices": {
            "T1": {"price_low": tp1, "price_high": tp1},
            "T2": {"price_low": tp2, "price_high": tp2},
        },
    }


def data(
    support_state,
    *,
    direction="SHORT",
    price=98.5,
    tp1=None,
    tp2=None,
    closed_price=99.0,
):
    return {
        "symbol": "XAUUSD",
        "normalized_analysis": {
            "supportState": support_state,
            "currentPrice": price,
            "lastClosedCandlePrice": closed_price,
            "lastClosedCandleTimestamp": NOW.isoformat(),
            "atr15": 10,
            "invalidationLevel": 100.2,
            "marketDataStatus": "GOOD",
            "consistencyValid": True,
            "entryQualityScore": 70,
            "confirmationLevels": [
                {"kind": "support", "timeframe": "15M", "price": 100, "buffer": 0.2}
            ],
            "tradingDecision": {
                "marketAssessment": {
                    "reversalState": "reversal_confirmed"
                    if direction == "LONG"
                    else "none"
                }
            },
        },
        "short_scenario": scenario("SHORT", tp1=tp1, tp2=tp2),
        "long_scenario": scenario("LONG", tp1=tp1, tp2=tp2),
    }


def test_short_entry_watch_ready_and_triggered_keep_same_setup_id():
    watch = evaluate_entry_engine(data("confirmed_breakdown"), now=NOW)
    assert watch.plan.status == "SETUP_WATCH"
    assert watch.plan.risk_reward >= 1.5
    no_signal = frame((100, 100.5, 99.5, 100), (100.05, 100.2, 99.8, 100.1))
    ready = evaluate_entry_engine(
        data("confirmed_breakdown", price=100), watch.plan, m5_closed=no_signal, now=NOW
    )
    assert ready.plan.status == "ENTRY_READY"
    bearish = frame((100, 100.5, 99.5, 100), (100.2, 100.3, 99.7, 99.8))
    triggered = evaluate_entry_engine(
        data("confirmed_breakdown"), ready.plan, m5_closed=bearish, now=NOW
    )
    assert triggered.plan.status == "ENTRY_TRIGGERED"
    assert triggered.plan.setup_id == watch.plan.setup_id
    assert triggered.plan.trigger_timeframe == "5M"
    assert all(
        x is not None
        for x in (
            triggered.plan.suggested_entry,
            triggered.plan.stop_loss,
            triggered.plan.take_profit_1,
            triggered.plan.take_profit_2,
        )
    )
    assert "【可進場方向】做空" in triggered.message


def test_long_entry_is_symmetric_and_requires_closed_bullish_trigger():
    watch = evaluate_entry_engine(
        data("failed_breakdown", direction="LONG", price=100), now=NOW
    )
    assert watch.plan.direction == "LONG"
    bullish = frame((100, 100.5, 99.5, 100), (99.8, 100.3, 99.7, 100.2))
    triggered = evaluate_entry_engine(
        data("failed_breakdown", direction="LONG", price=100.2, closed_price=100.2),
        watch.plan,
        m5_closed=bullish,
        now=NOW,
    )
    assert triggered.plan.status == "ENTRY_TRIGGERED"
    assert triggered.plan.risk_reward >= 1.5
    assert "【可進場方向】做多" in triggered.message


def test_insufficient_rr_never_creates_setup_or_signal():
    result = evaluate_entry_engine(
        data("confirmed_breakdown", tp1=99.0, tp2=98.5), now=NOW
    )
    assert result.plan.status == "NO_SETUP"
    assert result.should_notify is False


def test_closed_candle_beyond_stop_invalidates_before_entry():
    watch = evaluate_entry_engine(data("confirmed_breakdown"), now=NOW).plan
    invalid = evaluate_entry_engine(
        data("confirmed_breakdown", price=102, closed_price=102), watch, now=NOW
    )
    assert invalid.plan.status == "INVALIDATED"
    assert invalid.should_notify is True


def test_touch_without_reversal_does_not_enter_and_duplicate_is_suppressed():
    watch = evaluate_entry_engine(data("confirmed_breakdown"), now=NOW).plan
    no_signal = frame((100, 100.5, 99.5, 100), (100.05, 100.2, 99.8, 100.1))
    ready = evaluate_entry_engine(
        data("confirmed_breakdown", price=100), watch, m5_closed=no_signal, now=NOW
    )
    duplicate = evaluate_entry_engine(
        data("confirmed_breakdown", price=100), ready.plan, m5_closed=no_signal, now=NOW
    )
    assert ready.plan.status == "ENTRY_READY"
    assert ready.plan.setup_id == watch.setup_id
    assert duplicate.plan.status == "ENTRY_READY"
    assert duplicate.should_notify is False


def test_triggered_plan_remains_managed_after_first_target():
    watch = evaluate_entry_engine(data("confirmed_breakdown"), now=NOW).plan
    bearish = frame((100, 100.5, 99.5, 100), (100.2, 100.3, 99.7, 99.8))
    triggered = evaluate_entry_engine(
        data("confirmed_breakdown"), watch, m5_closed=bearish, now=NOW
    ).plan
    managed = evaluate_entry_engine(
        data("confirmed_breakdown", price=96.9), triggered, now=NOW
    )
    assert managed.plan.status == "ENTRY_TRIGGERED"
    assert managed.should_notify is False
    assert managed.message == ""


def test_trigger_clears_missing_condition_and_orders_short_targets():
    watch = evaluate_entry_engine(
        data("confirmed_breakdown", tp1=97, tp2=90), now=NOW
    ).plan
    bearish = frame((100, 100.5, 99.5, 100), (100.2, 100.3, 99.7, 99.8))
    triggered = evaluate_entry_engine(
        data("confirmed_breakdown", tp1=97, tp2=90),
        watch,
        m5_closed=bearish,
        now=NOW,
    ).plan
    assert triggered.status == "ENTRY_TRIGGERED"
    assert triggered.missing_condition == ""
    targets = [triggered.take_profit_1, triggered.take_profit_2,
               triggered.take_profit_3]
    targets = [value for value in targets if value is not None]
    assert targets == sorted(targets, reverse=True)
    assert validate_executable_plan(triggered) == (True, "")


def test_target_normalizer_rejects_wrong_side_and_orders_by_execution():
    assert ordered_profit_targets("SHORT", 4515.26, 4506.75, 4480.16, 4502.26) == (
        4506.75, 4502.26, 4480.16
    )
    assert ordered_profit_targets("LONG", 100, 105, 99, 103) == (103.0, 105.0, None)


def test_executable_plan_with_missing_condition_fails_closed():
    plan = EntryPlan(
        status="ENTRY_TRIGGERED", direction="SHORT", suggested_entry=100,
        stop_loss=101, take_profit_1=98, risk_reward=2,
        missing_condition="尚缺反轉 K 線",
    )
    assert validate_executable_plan(plan) == (False, "進場條件仍有缺項")


def test_15m_reversal_cannot_replace_required_closed_5m_trigger():
    watch = evaluate_entry_engine(
        data("failed_breakdown", direction="LONG", price=100), now=NOW
    ).plan
    bullish = frame((100, 100.5, 99.5, 100), (99.8, 100.3, 99.7, 100.2))
    result = evaluate_entry_engine(
        data("failed_breakdown", direction="LONG", price=100.2), watch,
        m15_closed=bullish, now=NOW)
    assert result.plan.status == "SETUP_WATCH"
    assert result.should_notify is False


def test_triggered_entry_has_traceable_quality_dimensions_not_probability():
    watch = evaluate_entry_engine(data("confirmed_breakdown"), now=NOW).plan
    bearish = frame((100, 100.5, 99.5, 100), (100.2, 100.3, 99.7, 99.8))
    triggered = evaluate_entry_engine(
        data("confirmed_breakdown"), watch, m5_closed=bearish, now=NOW)
    assert triggered.plan.status == "ENTRY_TRIGGERED"
    assert triggered.plan.entry_quality_score >= 70
    assert set(triggered.plan.entry_quality_breakdown or {}) == {
        "structure", "location", "momentum", "risk_reward", "execution", "freshness"}
    assert "不是勝率" in triggered.message


def test_confirmed_5m_too_far_from_zone_is_wait_retest_not_entry():
    watch = evaluate_entry_engine(
        data("confirmed_breakdown", tp1=90, tp2=85), now=NOW).plan
    chased = frame((100, 100.5, 99.5, 100), (100.2, 100.3, 95.5, 96.0))
    result = evaluate_entry_engine(
        data("confirmed_breakdown", tp1=90, tp2=85), watch,
        m5_closed=chased, now=NOW)
    assert result.plan.status == "ENTRY_READY"
    assert "超過最大追價距離" in result.plan.missing_condition


def test_setup_expiry_is_capped_at_four_15m_bars():
    watch = evaluate_entry_engine(data("confirmed_breakdown"), now=NOW).plan
    assert watch.expiry_bars == 4
    assert datetime.fromisoformat(watch.expires_at) - NOW == timedelta(minutes=60)


def test_low_quality_from_poor_location_and_execution_cost_blocks_entry():
    source = data("confirmed_breakdown", tp1=90, tp2=85)
    watch = evaluate_entry_engine(source, now=NOW).plan
    source["current_price"] = {"spread": 2.0}
    weak_location = frame(
        (100, 100.5, 99.5, 100), (100.2, 100.3, 96.3, 96.8))
    result = evaluate_entry_engine(
        source, watch, m5_closed=weak_location, now=NOW)
    assert result.plan.status == "ENTRY_READY"
    assert result.plan.entry_quality_score < 70
    assert "短線進場品質" in result.plan.missing_condition

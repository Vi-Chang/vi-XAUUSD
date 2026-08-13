from datetime import datetime, timedelta, timezone

from app.engines.event_reaction import assess_event_reaction
from app.services import event_service as es


def test_complete_event_result_calculates_surprise_without_fabricating_values():
    result = assess_event_reaction(
        post_event_wait=True, m15_closed_at="2026-08-13T12:30:00Z", macd_hist=0.5,
        dxy_chg_pct=-0.2, us10y_chg=-0.03, actual=3.2, forecast=3.0,
        previous=2.9, outcome_source="calendar-provider", event_name="US CPI (YoY)")
    assert result.outcome_status == "available"
    assert result.surprise == 0.2
    assert result.fundamental_bias == "bearish_xauusd"


def test_missing_actual_never_claims_a_fundamental_direction():
    result = assess_event_reaction(
        post_event_wait=True, m15_closed_at="2026-08-13T12:30:00Z", macd_hist=0.5,
        dxy_chg_pct=-0.2, us10y_chg=-0.03, forecast=3.0, previous=2.9,
        event_name="US CPI (YoY)")
    assert result.outcome_status == "pending"
    assert result.fundamental_bias == "unknown"


def test_event_outcome_is_exposed_only_when_catalog_contains_actual_and_forecast(monkeypatch):
    now = datetime(2026, 8, 13, 12, 40, tzinfo=timezone.utc)
    event = {"name": "Consumer Price Index", "country": "US", "impact": "HIGH",
             "time_utc": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
             "actual": "3.2%", "forecast": "3.0%", "previous": "2.9%",
             "outcome_source": "provider"}
    monkeypatch.setattr(es, "load_official_events", lambda _: ([event], False, now.isoformat()))
    monkeypatch.setattr(es, "load_manual_events", lambda: ([], True))
    state = es.evaluate_event_risk(now)
    assert (state.actual, state.forecast, state.previous) == (3.2, 3.0, 2.9)
    assert state.outcome_status == "available"
    assert state.outcome_source == "provider"

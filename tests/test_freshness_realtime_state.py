from datetime import datetime, timezone

from app.engines.freshness_state import evaluate_freshness_state
from app.engines.realtime_presentation import (
    build_realtime_presentation,
    validate_trade_prices,
)
from app.utils.timeutils import parse_utc, to_taipei

NOW = datetime(2026, 8, 24, 0, 16, tzinfo=timezone.utc)


def fixture(*, price=4639.21, closed=4624.27, trigger=4624.28, direction="LONG"):
    return {
        "symbol": "XAUUSD",
        "timestamp_utc": "2026-08-24T00:16:00Z",
        "current_price": {"mid": price, "last_update": "2026-08-24T00:14:00Z"},
        "event_risk": {"data_updated_at": "2026-08-24T00:10:00Z"},
        "normalized_analysis": {
            "currentPrice": price,
            "marketDataTimestamp": "2026-08-24T00:14:00Z",
            "lastClosedCandleTimestamp": "2026-08-24T00:00:00Z",
            "lastClosedCandlePrice": closed,
            "marketDataStatus": "GOOD",
        },
        "breakout_setup_manager": {"setups": [{
            "setupId": "BO-current", "direction": direction,
            "status": "WAIT_BREAKOUT_CONFIRMATION", "breakoutTrigger": trigger,
            "entryZoneLow": 4622.42, "entryZoneHigh": 4626.13,
            "maxChasePrice": 4630.0, "stopPrice": 4615.1,
            "tp1": 4648.0, "tp2": 4660.0, "tp3": 4672.0,
        }]},
    }


def test_utc_freshness_is_two_minutes_not_eight_hours():
    state = evaluate_freshness_state(fixture(), now=NOW)
    assert state["marketFreshness"]["status"] == "fresh"
    assert state["marketFreshness"]["ageSeconds"] == 120


def test_taipei_conversion_preserves_age():
    latest = parse_utc("2026-08-24T00:14:00Z")
    assert to_taipei(NOW).hour == 8 and to_taipei(latest).hour == 8
    assert (to_taipei(NOW) - to_taipei(latest)).total_seconds() == 120


def test_naive_legacy_timestamp_is_interpreted_at_utc_boundary():
    assert parse_utc("2026-08-24T00:14:00").tzinfo == timezone.utc


def test_realtime_price_and_distances_use_latest_quote():
    state = build_realtime_presentation(fixture(), now=NOW)
    assert state["currentPrice"] == 4639.21
    assert state["distanceToEntry"] == 13.08
    assert state["distanceToTrigger"] == -14.93


def test_intrabar_breakout_waits_for_close():
    state = build_realtime_presentation(fixture(), now=NOW)
    assert state["intrabarCrossed"] is True
    assert state["closedConfirmed"] is False
    assert state["triggerState"] == "WAIT_CLOSE_CONFIRMATION"


def test_closed_candle_confirms_breakout():
    state = build_realtime_presentation(fixture(closed=4625), now=NOW)
    assert state["closedConfirmed"] is True
    assert state["triggerState"] == "BREAKOUT_CONFIRMED"


def test_confirmed_breakout_too_far_waits_retest():
    state = build_realtime_presentation(fixture(closed=4625), now=NOW)
    assert state["opportunityState"] == "WAIT_RETEST"


def test_price_inside_entry_zone_distance_is_zero():
    state = build_realtime_presentation(fixture(price=4624), now=NOW)
    assert state["distanceToEntry"] == 0


def test_next_15m_close_boundary_and_countdown():
    state = build_realtime_presentation(fixture(), now=NOW)
    assert state["nextCandleCloseAtUtc"] == "2026-08-24T00:30:00Z"
    assert state["secondsToCandleClose"] == 840


def test_freshness_recovers_when_new_quote_arrives():
    old = fixture()
    old["current_price"]["last_update"] = "2026-08-23T20:00:00Z"
    old["normalized_analysis"]["marketDataTimestamp"] = "2026-08-23T20:00:00Z"
    assert evaluate_freshness_state(old, now=NOW)["marketFreshness"]["status"] == "stale"
    assert evaluate_freshness_state(fixture(), now=NOW)["marketFreshness"]["status"] == "fresh"


def test_health_state_distinguishes_disconnect_and_recovery(monkeypatch):
    monkeypatch.setattr("app.engines.freshness_state.market_is_open", lambda _now: True)
    missing = fixture()
    missing["current_price"] = {}
    missing["normalized_analysis"]["marketDataTimestamp"] = ""
    assert evaluate_freshness_state(missing, now=NOW)["healthState"] == "DISCONNECTED"
    recovered = evaluate_freshness_state(
        fixture(), now=NOW, previous_health_state="STALE")
    assert recovered["healthState"] == "RECOVERING"


def test_market_closed_is_not_reported_as_stale(monkeypatch):
    monkeypatch.setattr("app.engines.freshness_state.market_is_open", lambda _now: False)
    state = evaluate_freshness_state(fixture(), now=NOW)
    assert state["healthState"] == "MARKET_CLOSED"
    assert state["marketFreshness"]["healthState"] == "MARKET_CLOSED"


def test_duplicate_scenarios_are_suppressed_deterministically():
    data = fixture()
    duplicate = dict(data["breakout_setup_manager"]["setups"][0])
    data["breakout_setup_manager"]["setups"].append(duplicate)
    state = build_realtime_presentation(data, now=NOW)
    assert len(state["dedupedScenarios"]) == 1


def test_long_price_invariant():
    assert validate_trade_prices("LONG", 4626.13, 4615.1, [4648, 4660, 4672])
    assert not validate_trade_prices("LONG", 4626.13, 4640, [4648])


def test_short_price_invariant():
    assert validate_trade_prices("SHORT", 4622, 4635, [4600, 4590])
    assert not validate_trade_prices("SHORT", 4622, 4610, [4600])


def test_short_defense_crossed_is_explicit():
    data = fixture(direction="SHORT", trigger=4645)
    data["breakout_setup_manager"]["setups"][0].update(
        entryZoneLow=4620, entryZoneHigh=4624, maxChasePrice=4610,
        stopPrice=4626.13, tp1=4600, tp2=4590, tp3=4580)
    state = build_realtime_presentation(data, now=NOW)
    assert state["defenseState"] == "POSITION_DEFENSE_TRIGGERED"


def test_stale_ai_snapshot_cannot_override_realtime_price():
    data = fixture()
    data["ai_strategy"] = {"currentPrice": 4510, "triggerState": "WAIT"}
    state = build_realtime_presentation(data, price=4639.21, now=NOW)
    assert state["currentPrice"] == 4639.21
    assert state["intrabarCrossed"] is True


def test_effective_rr_is_computed_from_frozen_setup():
    state = build_realtime_presentation(fixture(), now=NOW)
    expected = round((4648 - 4626.13) / (4626.13 - 4615.1), 2)
    assert state["effectiveRR"] == expected


def test_fact_version_ignores_countdown_only_changes():
    first = build_realtime_presentation(fixture(), now=NOW)
    later = build_realtime_presentation(
        fixture(), now=datetime(2026, 8, 24, 0, 16, 5, tzinfo=timezone.utc))
    assert first["factVersion"] == later["factVersion"]

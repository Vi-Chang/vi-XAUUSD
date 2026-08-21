from app.db.session import init_db
from app.services.market_monitor_service import evaluate_live_quote_state


def test_refresh_rehydrates_state_without_repeating_last_transition():
    init_db()
    data = {
        "symbol": "XAUUSD-RELOAD-TEST",
        "entry_engine": {
            "status": "SETUP_WATCH",
            "direction": "LONG",
            "missing_condition": "等待 15M 收盤確認",
        },
        "market_decision": {"action": "PREPARE_LONG", "reason": "等待確認"},
        "normalized_analysis": {
            "currentPrice": 4481,
            "marketDataTimestamp": "2026-08-20T14:00:00+00:00",
            "lastClosedCandleTimestamp": "2026-08-20T13:45:00+00:00",
            "marketDataStatus": "GOOD",
            "consistencyValid": True,
            "confirmationLevels": [
                {"kind": "resistance", "timeframe": "15M", "price": 4490}
            ],
        },
    }
    first, events = evaluate_live_quote_state(
        data, price=4481, quote_time="2026-08-20T14:00:00+00:00"
    )
    assert first["state"] == "LONG_BIAS"
    assert any(event["event_type"] == "STATE_CHANGED" for event in events)

    restored, repeated = evaluate_live_quote_state(
        data, price=4481, quote_time="2026-08-20T14:00:00+00:00"
    )
    assert restored["state"] == "LONG_BIAS"
    assert restored["last_event"] == first["last_event"]
    assert repeated == []

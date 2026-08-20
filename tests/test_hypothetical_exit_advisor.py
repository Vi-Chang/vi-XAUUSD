from app.engines.hypothetical_exit_advisor import evaluate_hypothetical_exits


def data(price=4497, closed=4497, entry_status="NO_SETUP"):
    return {
        "entry_engine": {"status": entry_status},
        "position_management": {"has_position": False},
        "normalized_analysis": {
            "currentPrice": price,
            "lastClosedCandlePrice": closed,
            "lastClosedCandleTimestamp": "2026-08-20T13:00:00+00:00",
            "atr15": 5,
            "confirmationLevels": [
                {"kind": "support", "timeframe": "15M", "price": 4490.27, "buffer": 0},
                {"kind": "resistance", "timeframe": "15M", "price": 4508, "buffer": 0},
            ],
        },
        "key_levels": {
            "strong_resistance_zones": [
                {"price_low": 4499, "price_high": 4500},
                {"price_low": 4507, "price_high": 4509},
            ],
            "strong_support_zones": [
                {"price_low": 4489, "price_high": 4491},
                {"price_low": 4478, "price_high": 4480},
            ],
        },
    }


def test_no_position_still_gets_conditional_exit_advice():
    _state, events = evaluate_hypothetical_exits(data(price=4497))
    assert events[0]["event_type"] == "EXIT_APPROACHING"
    assert "若你持有多單" in events[0]["message"]


def test_invalidated_or_missed_entry_does_not_disable_exit_advisor():
    for status in ("INVALIDATED", "EXITED"):
        _state, events = evaluate_hypothetical_exits(
            data(price=4499.5, entry_status=status)
        )
        assert any(e["event_type"] == "EXIT_ZONE_REACHED" for e in events)


def test_price_up_then_closed_below_defense_emits_exit_now():
    state, _ = evaluate_hypothetical_exits(data(price=4499.5))
    _state, events = evaluate_hypothetical_exits(data(price=4488, closed=4488), state)
    event = next(e for e in events if e["side"] == "LONG")
    assert event["event_type"] == "EXIT_NOW"
    assert event["action"] == "全部平倉"


def test_leaving_and_reentering_zone_can_notify_again():
    inside, first = evaluate_hypothetical_exits(data(price=4499.5))
    outside, _ = evaluate_hypothetical_exits(data(price=4495), inside)
    _again, second = evaluate_hypothetical_exits(data(price=4499.5), outside)
    assert sum(e["event_type"] == "EXIT_ZONE_REACHED" for e in first) == 1
    assert sum(e["event_type"] == "EXIT_ZONE_REACHED" for e in second) == 1
    assert first[0]["topic"] != second[0]["topic"]

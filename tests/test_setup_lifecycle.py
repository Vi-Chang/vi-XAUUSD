from app.engines.setup_lifecycle import evaluate_setup_lifecycle

BASE = {
    "setup_id": "xau-long-4539",
    "direction": "LONG",
    "confirmation_price": 4539.17,
    "closed_candle_time": "2026-08-21T02:45:00+00:00",
    "entry_zone_low": 4538.5,
    "entry_zone_high": 4540.0,
    "risk_controls_passed": True,
    "calculated_at": "2026-08-21T03:01:00+00:00",
}


def evaluate(*, previous=None, close=4538.0, price=4534.32, **changes):
    values = {**BASE, **changes}
    return evaluate_setup_lifecycle(
        previous=previous,
        latest_closed_price=close,
        current_price=price,
        invalidated=False,
        **values,
    )


def test_below_confirmation_at_4534_stays_wait_confirmation():
    result = evaluate(price=4534.32)
    assert result["state"] == "WAIT_CONFIRMATION"
    assert result["confirmedAt"] is None


def test_intrabar_move_to_4537_does_not_become_missed_or_change_state():
    first = evaluate(price=4534.32)
    second = evaluate(previous=first, price=4537.02)
    assert second["state"] == "WAIT_CONFIRMATION"
    assert second["missedAt"] is None


def test_intrabar_break_above_but_closed_below_stays_wait_confirmation():
    result = evaluate(price=4541.0, close=4538.9)
    assert result["state"] == "WAIT_CONFIRMATION"


def test_closed_above_inside_zone_and_controls_pass_becomes_entry_ready():
    result = evaluate(price=4539.5, close=4540.0)
    assert result["state"] == "ENTRY_READY"
    assert result["wasEntryReady"] is True
    assert result["confirmedCandleTime"] == BASE["closed_candle_time"]


def test_closed_above_but_far_from_zone_waits_retest_not_missed():
    result = evaluate(price=4548.0, close=4545.0)
    assert result["state"] == "CONFIRMED_WAIT_RETEST"
    assert result["missedAt"] is None


def test_only_notified_prior_entry_ready_can_become_missed():
    ready = evaluate(price=4539.5, close=4540.0)
    not_notified = evaluate(previous=ready, price=4548.0, close=4545.0)
    assert not_notified["state"] == "ENTRY_READY"
    notified = {**ready, "entryNotificationSentAt": "2026-08-21T03:01:05+00:00"}
    missed = evaluate(previous=notified, price=4548.0, close=4545.0)
    assert missed["state"] == "MISSED_ENTRY"
    assert missed["missedAt"] is not None


def test_illegal_wait_to_missed_is_impossible_even_with_stale_fields():
    corrupt = {
        "setupId": BASE["setup_id"], "state": "WAIT_CONFIRMATION",
        "wasEntryReady": True, "entryNotificationSentAt": "2026-08-21T03:00:00Z",
    }
    result = evaluate(previous=corrupt, price=4537.02, close=4538.0)
    assert result["state"] == "WAIT_CONFIRMATION"

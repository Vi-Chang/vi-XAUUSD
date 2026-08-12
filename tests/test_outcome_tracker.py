from datetime import datetime, timedelta, timezone

from app.services.outcome_tracker import first_close_at_or_after, signed_return_pct


def test_long_forward_return_is_directional():
    assert signed_return_pct("LONG", 4000, 4040) == 1.0
    assert signed_return_pct("LONG", 4000, 3960) == -1.0


def test_short_forward_return_is_directional():
    assert signed_return_pct("SHORT", 4000, 3960) == 1.0
    assert signed_return_pct("PREPARE_SHORT", 4000, 4040) == -1.0


def test_non_entry_action_has_no_outcome():
    assert signed_return_pct("WATCH", 4000, 4040) is None
    assert signed_return_pct("LONG", 0, 4040) is None


def test_each_horizon_uses_its_own_first_closed_candle():
    start = datetime(2026, 8, 13, tzinfo=timezone.utc)
    candles = [(start + timedelta(minutes=15), 4010.0),
               (start + timedelta(hours=1), 4040.0),
               (start + timedelta(hours=4), 3960.0)]
    assert first_close_at_or_after(candles, start + timedelta(minutes=15)) == 4010.0
    assert first_close_at_or_after(candles, start + timedelta(hours=1)) == 4040.0
    assert first_close_at_or_after(candles, start + timedelta(hours=4)) == 3960.0


def test_missing_future_candle_stays_unfilled():
    start = datetime(2026, 8, 13, tzinfo=timezone.utc)
    candles = [(start + timedelta(minutes=15), 4010.0)]
    assert first_close_at_or_after(candles, start + timedelta(hours=1)) is None

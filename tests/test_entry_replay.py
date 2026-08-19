from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.services.entry_replay import ReplayStep, replay_entry_engine
from tests.test_entry_engine import data


NOW = datetime(2026, 8, 13, 1, tzinfo=timezone.utc)


def candles(*rows):
    return pd.DataFrame(rows, columns=("open", "high", "low", "close", "is_closed"))


def test_replay_waits_for_closed_reversal_before_triggering():
    watch = ReplayStep(NOW, data("confirmed_breakdown"))
    touch_only = ReplayStep(NOW + timedelta(minutes=5), data("confirmed_breakdown", price=100),
        m5_closed=candles((100, 100.5, 99.5, 100, True),
                          (100.05, 100.2, 99.8, 100.1, True)))
    confirmed = ReplayStep(NOW + timedelta(minutes=10), data("confirmed_breakdown", price=99),
        m5_closed=candles((100, 100.5, 99.5, 100, True),
                          (100.2, 100.3, 99.7, 99.8, True)))
    transitions = replay_entry_engine((watch, touch_only, confirmed))
    assert [item.new_status for item in transitions] == [
        "SETUP_WATCH", "ENTRY_READY", "ENTRY_TRIGGERED"]
    assert transitions[-1].at == NOW + timedelta(minutes=10)


def test_replay_rejects_out_of_order_data():
    with pytest.raises(ValueError, match="chronological"):
        replay_entry_engine((ReplayStep(NOW, data("none")),
                             ReplayStep(NOW - timedelta(minutes=1), data("none"))))

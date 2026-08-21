from datetime import datetime, timedelta, timezone

import pandas as pd

from app.services.trend_continuation_replay import replay_continuation


def bars(count, minutes, step):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return pd.DataFrame([{"open_time": start + timedelta(minutes=minutes * i),
        "close_time": start + timedelta(minutes=minutes * (i + 1)),
        "open": 100 + i * step, "high": 100.5 + i * step,
        "low": 99.5 + i * step, "close": 100.3 + i * step,
        "volume": 100, "is_closed": True} for i in range(count)])


def test_replay_is_chronological_and_records_mfe_mae_without_using_future_as_input():
    m15, h1, h4 = bars(60, 15, .3), bars(60, 60, .7), bars(60, 240, 1.0)
    seen = []

    def factory(bar):
        seen.append(bar["close_time"])
        return {"normalized_analysis": {"marketDataStatus": "GOOD"},
                "timeframes": {"m15": {"rsi": 60}}}

    result = replay_continuation(m15, h1, h4, data_factory=factory)
    assert result["bars"] == 41
    assert seen == sorted(seen)
    assert all("mfe" in row and "mae" in row and "rejectedReasons" in row
               for row in result["records"])
    assert result["summary"]["duplicateNotifications"] == 0

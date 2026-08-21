"""Print a no-lookahead continuation replay from locally retained candles."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.trend_continuation_replay import replay_continuation


def load(connection, timeframe: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        "SELECT open_time,close_time,open,high,low,close,volume,is_closed "
        "FROM candles WHERE timeframe=? AND is_closed=1 ORDER BY close_time",
        connection, params=(timeframe,))
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
    return frame


def main() -> None:
    connection = sqlite3.connect("xauusd.db")
    m15, h1, h4 = (load(connection, timeframe) for timeframe in ("15M", "1H", "4H"))

    def factory(bar):
        return {"symbol": "XAUUSD", "timestamp_utc": str(bar["close_time"]),
                "normalized_analysis": {"marketDataStatus": "GOOD"},
                "timeframes": {"m15": {"rsi": 50}}}

    result = replay_continuation(m15, h1, h4, data_factory=factory)
    compact = {"retainedRanges": {
        "15M": [str(m15["open_time"].min()), str(m15["close_time"].max()), len(m15)],
        "1H": [str(h1["open_time"].min()), str(h1["close_time"].max()), len(h1)],
        "4H": [str(h4["open_time"].min()), str(h4["close_time"].max()), len(h4)]},
        "summary": result["summary"],
        "readyCandidates": [row for row in result["records"]
                            if str(row["decision"]).startswith("ENTRY_READY_")],
        "august21Candidates": [row for row in result["records"]
                               if str(row["candleTime"]).startswith("2026-08-21")]}
    if "--summary-only" in sys.argv:
        compact = {"retainedRanges": compact["retainedRanges"],
                   "summary": compact["summary"],
                   "readyCandidates": compact["readyCandidates"]}
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

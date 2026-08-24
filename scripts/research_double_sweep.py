"""Reproducible DOUBLE_SWEEP research export from retained closed 15M candles."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engines.double_sweep import DoubleSweepConfig, detect_double_sweeps
from app.services.double_sweep_research import (
    aggregate_outcomes,
    build_point_in_time_outcomes,
    chronological_split,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=str(ROOT / "xauusd.db"))
    parser.add_argument(
        "--output", default=str(ROOT / "data" / "double_sweep_profile.json")
    )
    args = parser.parse_args()
    with sqlite3.connect(args.database) as connection:
        candles = pd.read_sql_query(
            "SELECT close_time,open,high,low,close,volume,is_closed FROM candles "
            "WHERE symbol='XAUUSD' AND timeframe='15M' AND is_closed=1 ORDER BY close_time",
            connection,
        )
    if candles.empty:
        raise SystemExit("No closed XAUUSD 15M candles; profile was not fabricated.")
    candles["close_time"] = pd.to_datetime(candles["close_time"], utc=True)
    candles = candles.set_index("close_time")
    config = DoubleSweepConfig()
    events = [event.to_dict() for event in detect_double_sweeps(candles, config=config)]
    rows = build_point_in_time_outcomes(candles, events)
    splits = chronological_split(rows)
    dataset_version = f"{candles.index.min().isoformat()}..{candles.index.max().isoformat()}:{len(candles)}"
    payload = aggregate_outcomes(
        rows, dataset_version=dataset_version, config=config.__dict__
    )
    payload["splitCounts"] = {key: len(value) for key, value in splits.items()}
    payload["lookaheadPolicy"] = (
        "closed-candle-only; frozen pre-sweep range; outcomes after confirmedAt"
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "events": len(rows),
                "splitCounts": payload["splitCounts"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

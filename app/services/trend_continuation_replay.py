"""No-lookahead replay and outcome audit for the continuation engine."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from app.engines.trend_continuation_engine import evaluate_trend_continuation


def replay_continuation(
    m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame,
    *, data_factory: Callable[[pd.Series], dict], outcome_bars: int = 8,
) -> dict:
    records: list[dict[str, Any]] = []
    previous: dict[str, Any] = {}
    ordered = m15.sort_values("close_time").reset_index(drop=True)
    for index in range(19, len(ordered)):
        visible = ordered.iloc[:index + 1]
        bar = visible.iloc[-1]
        cutoff = bar["close_time"]
        visible_h1 = h1[h1["close_time"] <= cutoff]
        visible_h4 = h4[h4["close_time"] <= cutoff]
        state, events = evaluate_trend_continuation(
            data_factory(bar), m15=visible, h1=visible_h1, h4=visible_h4,
            previous=previous)
        previous = state
        selected = state.get("selected")
        candidates = state.get("candidates") or []
        primary: dict[str, Any] = selected or next((item for item in candidates
                                                    if item.get("type") == "SHALLOW_PULLBACK_LONG"), {})
        future = ordered.iloc[index + 1:index + 1 + outcome_bars]
        entry = primary.get("suggestedEntry")
        stop = primary.get("stopPrice")
        entry_value = float(entry) if isinstance(entry, (int, float)) else 0.0
        stop_value = float(stop) if isinstance(stop, (int, float)) else 0.0
        risk = entry_value - stop_value if entry_value and stop_value else 0.0
        mfe = (float(future["high"].max()) - entry_value) if risk > 0 and not future.empty else 0.0
        mae = (entry_value - float(future["low"].min())) if risk > 0 and not future.empty else 0.0
        outcome, realized_r, bars_to_outcome = "OPEN", 0.0, None
        if risk > 0:
            for future_index, (_, future_bar) in enumerate(future.iterrows(), 1):
                # OHLC cannot reveal intrabar ordering; use conservative stop-first.
                if float(future_bar["low"]) <= stop_value:
                    outcome, realized_r, bars_to_outcome = "STOP", -1.0, future_index
                    break
                if float(future_bar["high"]) >= float(primary.get("tp1") or 1e100):
                    outcome, realized_r, bars_to_outcome = "TP1", 1.5, future_index
                    break
            if outcome == "OPEN" and not future.empty:
                realized_r = round((float(future.iloc[-1]["close"]) - entry_value) / risk, 2)
        records.append({"candleTime": str(cutoff), "close": round(float(bar["close"]), 2),
                        "marketType": state.get("marketType"), "trendScore": state.get("trendScore"),
                        "decision": state.get("status"), "setupId": primary.get("setupId"),
                        "setupType": primary.get("type"),
                        "entryZone": [primary.get("entryZoneLow"), primary.get("entryZoneHigh")],
                        "stop": stop, "targets": [primary.get("tp1"), primary.get("tp2"), primary.get("tp3")],
                        "estimatedRR": primary.get("riskReward"),
                        "rejectedReasons": primary.get("missingConditions") or [],
                        "mfe": round(mfe, 2), "mae": round(mae, 2),
                        "mfeR": round(mfe / risk, 2) if risk > 0 else None,
                        "maeR": round(mae / risk, 2) if risk > 0 else None,
                        "reached1R": bool(risk > 0 and mfe >= risk),
                        "reached2R": bool(risk > 0 and mfe >= 2 * risk),
                        "outcome": outcome, "realizedR": realized_r,
                        "barsToOutcome": bars_to_outcome,
                        "eventCount": len(events)})
    ready = [row for row in records if str(row["decision"]).startswith("ENTRY_READY_")]
    wins = [row for row in ready if row["outcome"] == "TP1"]
    missed = [row for row in records if row["decision"] == "WAIT" and row["reached1R"]]
    opportunities = [row for row in records if row["reached1R"]]
    caught = [row for row in ready if row["reached1R"]]
    quick_stops = [row for row in ready if row["outcome"] == "STOP"
                   and (row["barsToOutcome"] or 99) <= 2]
    equity, peak, maximum_drawdown = 0.0, 0.0, 0.0
    for row in ready:
        equity += float(row["realizedR"])
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    return {"bars": len(records), "records": records,
            "summary": {"signals": len(ready),
                        "winRate": round(len(wins) / len(ready), 4) if ready else None,
                        "averageR": round(sum(float(row["realizedR"]) for row in ready) / len(ready), 3) if ready else None,
                        "averageMfeR": round(sum(row["mfeR"] or 0 for row in ready) / len(ready), 3) if ready else None,
                        "maximumMaeR": max((row["maeR"] or 0 for row in ready), default=None),
                        "maximumDrawdownR": round(maximum_drawdown, 3),
                        "trendCaptureRate": round(len(caught) / len(opportunities), 4) if opportunities else None,
                        "waitThenOneRMissRate": round(len(missed) / len(records), 4) if records else None,
                        "quickStopRate": round(len(quick_stops) / len(ready), 4) if ready else None,
                        "legacyContinuationSignals": 0,
                        "duplicateNotifications": sum(max(0, row["eventCount"] - 1) for row in records)}}

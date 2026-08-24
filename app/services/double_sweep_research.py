"""Reproducible historical DOUBLE_SWEEP outcome aggregation."""

from __future__ import annotations

import hashlib
import json
import math
from statistics import median

import pandas as pd

HORIZONS = (1, 2, 4, 8, 16, 32)  # 15m, 30m, 1h, 2h, 4h, 8h


def build_point_in_time_outcomes(
    candles: pd.DataFrame, events: list[dict], *, touch_atr: float = 1.0
) -> list[dict]:
    """Attach only candles after confirmation; detector inputs remain untouched."""
    if candles.empty:
        return []
    frame = candles.copy().sort_index()
    if "is_closed" in frame.columns:
        frame = frame[frame["is_closed"].astype(bool)]
    index = pd.to_datetime(frame.index, utc=True)
    rows = []
    for event in events:
        confirmed = pd.Timestamp(event["confirmedAt"])
        if confirmed.tzinfo is None:
            confirmed = confirmed.tz_localize("UTC")
        positions = [i for i, value in enumerate(index) if value == confirmed]
        if not positions:
            continue
        start = positions[-1]
        base, unit = float(frame.iloc[start]["close"]), float(event["referenceAtr"])
        if unit <= 0:
            continue
        future = frame.iloc[start + 1 : start + 1 + max(HORIZONS)]
        outcomes = {}
        for horizon in HORIZONS:
            if len(future) < horizon:
                continue
            window = future.iloc[:horizon]
            close = float(window.iloc[-1]["close"])
            outcomes[str(horizon)] = {
                "returnAtr": round((close - base) / unit, 4),
                "mfeAtr": round((float(window["high"].max()) - base) / unit, 4),
                "maeAtr": round((base - float(window["low"].min())) / unit, 4),
            }
        first_direction, first_minutes = "NONE", None
        for offset, (_, candle) in enumerate(future.iterrows(), start=1):
            up = float(candle["high"]) >= base + touch_atr * unit
            down = float(candle["low"]) <= base - touch_atr * unit
            if up or down:
                first_direction = "BOTH" if up and down else "UP" if up else "DOWN"
                first_minutes = offset * 15
                break
        rows.append(
            {
                "eventId": event["eventId"],
                "order": event["order"],
                "confirmedAt": event["confirmedAt"],
                "horizons": outcomes,
                "firstTouchDirection": first_direction,
                "firstTouchMinutes": first_minutes,
                "mfeUpAtr": max((x["mfeAtr"] for x in outcomes.values()), default=0),
                "mfeDownAtr": max((x["maeAtr"] for x in outcomes.values()), default=0),
            }
        )
    return rows


def chronological_split(
    rows: list[dict], development: float = 0.60, validation: float = 0.20
) -> dict[str, list[dict]]:
    ordered = sorted(rows, key=lambda row: row["confirmedAt"])
    n = len(ordered)
    dev_end, validation_end = int(n * development), int(n * (development + validation))
    return {
        "development": ordered[:dev_end],
        "validation": ordered[dev_end:validation_end],
        "outOfSample": ordered[validation_end:],
    }


def wilson_interval(
    wins: int, total: int, z: float = 1.96
) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    den = 1 + z * z / total
    center = (p + z * z / (2 * total)) / den
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / den
    return round(max(0, center - margin), 4), round(min(1, center + margin), 4)


def sample_confidence(n: int) -> tuple[str, float]:
    if n < 20:
        return "LOW_CONFIDENCE", 0.0
    if n < 50:
        return "LIMITED", 0.35
    if n < 100:
        return "MODERATE", 0.65
    return "STRONGER_SAMPLE", 1.0


def aggregate_outcomes(rows: list[dict], *, dataset_version: str, config: dict) -> dict:
    """Aggregate already point-in-time-safe event outcomes without fitting direction."""
    profiles: dict[str, dict] = {}
    for order in ("HIGH_THEN_LOW", "LOW_THEN_HIGH"):
        subset = [row for row in rows if row.get("order") == order]
        n = len(subset)
        confidence, weight = sample_confidence(n)
        horizons = {}
        for bars in HORIZONS:
            key = str(bars)
            settled = [
                row["horizons"][key]
                for row in subset
                if key in (row.get("horizons") or {})
            ]
            wins = sum(float(item["returnAtr"]) > 0 for item in settled)
            low, high = wilson_interval(wins, len(settled))
            horizons[key] = {
                "sampleSize": len(settled),
                "probabilityUp": round(wins / len(settled), 4) if settled else None,
                "confidenceInterval95": [low, high],
                "medianReturnAtr": round(
                    median([float(x["returnAtr"]) for x in settled]), 4
                )
                if settled
                else None,
                "medianMfeAtr": round(median([float(x["mfeAtr"]) for x in settled]), 4)
                if settled
                else None,
                "medianMaeAtr": round(median([float(x["maeAtr"]) for x in settled]), 4)
                if settled
                else None,
            }
        up_first = sum(str(row.get("firstTouchDirection")) == "UP" for row in subset)
        down_first = sum(
            str(row.get("firstTouchDirection")) == "DOWN" for row in subset
        )
        directional = (
            "UP"
            if up_first > down_first
            else "DOWN"
            if down_first > up_first
            else "NONE"
        )
        trigger_times = sorted(
            float(row["firstTouchMinutes"])
            for row in subset
            if isinstance(row.get("firstTouchMinutes"), (int, float))
        )

        def percentile(q: float, values=trigger_times):
            if not values:
                return None
            return values[round((len(values) - 1) * q)]

        expected_values = [
            max(float(row.get("mfeUpAtr") or 0), float(row.get("mfeDownAtr") or 0))
            for row in subset
        ]
        profiles[order] = {
            "sampleSize": n,
            "sampleConfidence": confidence,
            "confidenceWeight": weight,
            "directionalBias": directional,
            "horizons": horizons,
            "expectedMoveAtr": round(median(expected_values), 4)
            if expected_values
            else None,
            "triggerTimeP25Min": percentile(0.25),
            "triggerTimeMedianMin": percentile(0.5),
            "triggerTimeP75Min": percentile(0.75),
        }
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode()
    ).hexdigest()[:16]
    return {
        "datasetVersion": dataset_version,
        "configHash": config_hash,
        "sampleCount": len(rows),
        "profiles": profiles,
        "researchVersion": "double-sweep-research-v1",
    }

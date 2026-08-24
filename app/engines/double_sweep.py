"""Deterministic, closed-candle-only DOUBLE_SWEEP detection and edge lifecycle."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal

import pandas as pd

from app.engines.execution_context import market_session
from app.engines.indicators import atr as atr_series

SweepOrder = Literal["HIGH_THEN_LOW", "LOW_THEN_HIGH"]


@dataclass(frozen=True)
class DoubleSweepConfig:
    reference_bars: int = 16
    min_depth_atr: float = 0.10
    max_interval_bars: int = 8
    max_reclaim_bars: int = 3


@dataclass(frozen=True)
class DoubleSweepEvent:
    eventId: str
    symbol: str
    eventType: str
    order: SweepOrder
    referenceHigh: float
    referenceLow: float
    referenceMid: float
    referenceRange: float
    referenceAtr: float
    highSweep: float
    lowSweep: float
    highSweepDepthAtr: float
    lowSweepDepthAtr: float
    sweepSymmetry: float
    firstSweepAt: str
    secondSweepAt: str
    reclaimAt: str
    confirmedAt: str
    reclaimStatus: str
    reclaimQuality: int
    reclaimBars: int
    reclaimSeconds: int
    barsBetweenSweeps: int
    timeBetweenSweepsSeconds: int
    regime4H: str
    structure1H: str
    session: str
    macroContext: str
    sourceCandleIds: tuple[str, ...]
    detectionVersion: str = "double-sweep-v1"

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["sourceCandleIds"] = list(self.sourceCandleIds)
        return payload


def _utc_iso(value) -> str:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    return parsed.tz_convert("UTC").isoformat()


def _seconds(a, b) -> int:
    return max(0, round((pd.Timestamp(b) - pd.Timestamp(a)).total_seconds()))


def _quality(
    *,
    close: float,
    ref_low: float,
    ref_high: float,
    second_side: str,
    reclaim_bars: int,
    max_reclaim_bars: int,
    wick_ratio: float,
) -> tuple[str, int]:
    # A close through the far side is an even stronger reclaim, not PARTIAL.
    inside = close >= ref_low if second_side == "LOW" else close <= ref_high
    crossed_mid = (
        close >= (ref_low + ref_high) / 2
        if second_side == "LOW"
        else close <= (ref_low + ref_high) / 2
    )
    if not inside:
        return "PARTIAL", max(1, min(49, round(wick_ratio * 25)))
    speed = max(0.0, 1 - reclaim_bars / max(max_reclaim_bars, 1))
    score = round(
        50 + 25 * min(1.0, wick_ratio) + 20 * speed + (5 if crossed_mid else 0)
    )
    return ("STRONG" if score >= 80 else "FULL"), min(100, score)


def detect_double_sweeps(
    candles: pd.DataFrame,
    *,
    symbol: str = "XAUUSD",
    regime4h: str = "UNKNOWN",
    structure1h: str = "UNKNOWN",
    macro_context: str = "NORMAL",
    config: DoubleSweepConfig | None = None,
) -> list[DoubleSweepEvent]:
    """Detect immutable events without future pivots or forming candles.

    The reference range uses only bars strictly before the first sweep. Once a
    first sweep is observed the range is frozen until confirmation/expiry.
    """
    cfg = config or DoubleSweepConfig()
    required = {"open", "high", "low", "close"}
    if candles.empty or not required.issubset(candles.columns):
        return []
    frame = candles.copy().sort_index()
    if "is_closed" in frame.columns:
        frame = frame[frame["is_closed"].astype(bool)]
    if len(frame) < cfg.reference_bars + 2:
        return []
    atr = atr_series(frame)
    events: list[DoubleSweepEvent] = []
    pending: dict | None = None
    for i in range(cfg.reference_bars, len(frame)):
        row = frame.iloc[i]
        at = frame.index[i]
        current_atr = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0.0
        if not math.isfinite(current_atr) or current_atr <= 0:
            continue
        if (
            pending
            and i - pending["firstIndex"] > cfg.max_interval_bars + cfg.max_reclaim_bars
        ):
            pending = None
        if pending is None:
            prior = frame.iloc[i - cfg.reference_bars : i]
            ref_high, ref_low = float(prior["high"].max()), float(prior["low"].min())
            high_depth = (float(row["high"]) - ref_high) / current_atr
            low_depth = (ref_low - float(row["low"])) / current_atr
            side = (
                "HIGH"
                if high_depth >= cfg.min_depth_atr
                else "LOW"
                if low_depth >= cfg.min_depth_atr
                else ""
            )
            if not side or (
                high_depth >= cfg.min_depth_atr and low_depth >= cfg.min_depth_atr
            ):
                continue
            pending = {
                "side": side,
                "firstIndex": i,
                "firstAt": at,
                "refHigh": ref_high,
                "refLow": ref_low,
                "atr": current_atr,
                "high": float(row["high"]),
                "low": float(row["low"]),
                "source": [_utc_iso(at)],
                "secondIndex": None,
            }
            continue
        ref_high, ref_low = pending["refHigh"], pending["refLow"]
        pending["high"] = max(pending["high"], float(row["high"]))
        pending["low"] = min(pending["low"], float(row["low"]))
        opposite = "LOW" if pending["side"] == "HIGH" else "HIGH"
        crossed = (
            opposite == "LOW"
            and float(row["low"]) <= ref_low - cfg.min_depth_atr * pending["atr"]
        ) or (
            opposite == "HIGH"
            and float(row["high"]) >= ref_high + cfg.min_depth_atr * pending["atr"]
        )
        if pending["secondIndex"] is None:
            if not crossed or i - pending["firstIndex"] > cfg.max_interval_bars:
                continue
            pending["secondIndex"], pending["secondAt"] = i, at
            pending["source"].append(_utc_iso(at))
        reclaim_bars = i - pending["secondIndex"]
        close = float(row["close"])
        reclaimed = close >= ref_low if opposite == "LOW" else close <= ref_high
        if not reclaimed:
            if reclaim_bars >= cfg.max_reclaim_bars:
                pending = None
            continue
        body = abs(float(row["close"]) - float(row["open"]))
        full_range = max(float(row["high"]) - float(row["low"]), 1e-9)
        wick_ratio = max(0.0, min(1.0, (full_range - body) / full_range))
        status, quality = _quality(
            close=close,
            ref_low=ref_low,
            ref_high=ref_high,
            second_side=opposite,
            reclaim_bars=reclaim_bars,
            max_reclaim_bars=cfg.max_reclaim_bars,
            wick_ratio=wick_ratio,
        )
        high_depth = max(0.0, (pending["high"] - ref_high) / pending["atr"])
        low_depth = max(0.0, (ref_low - pending["low"]) / pending["atr"])
        symmetry = min(high_depth, low_depth) / max(high_depth, low_depth, 1e-9)
        order: SweepOrder = (
            "HIGH_THEN_LOW" if pending["side"] == "HIGH" else "LOW_THEN_HIGH"
        )
        confirmed = _utc_iso(at)
        seed = f"{symbol}|{order}|{pending['source'][0]}|{pending['source'][-1]}|{ref_high:.5f}|{ref_low:.5f}"
        event = DoubleSweepEvent(
            eventId=f"DS-{hashlib.sha256(seed.encode()).hexdigest()[:20]}",
            symbol=symbol,
            eventType="DOUBLE_SWEEP",
            order=order,
            referenceHigh=round(ref_high, 5),
            referenceLow=round(ref_low, 5),
            referenceMid=round((ref_high + ref_low) / 2, 5),
            referenceRange=round(ref_high - ref_low, 5),
            referenceAtr=round(pending["atr"], 5),
            highSweep=round(pending["high"], 5),
            lowSweep=round(pending["low"], 5),
            highSweepDepthAtr=round(high_depth, 4),
            lowSweepDepthAtr=round(low_depth, 4),
            sweepSymmetry=round(symmetry, 4),
            firstSweepAt=_utc_iso(pending["firstAt"]),
            secondSweepAt=_utc_iso(pending["secondAt"]),
            reclaimAt=confirmed,
            confirmedAt=confirmed,
            reclaimStatus=status,
            reclaimQuality=quality,
            reclaimBars=reclaim_bars,
            reclaimSeconds=_seconds(pending["secondAt"], at),
            barsBetweenSweeps=pending["secondIndex"] - pending["firstIndex"],
            timeBetweenSweepsSeconds=_seconds(pending["firstAt"], pending["secondAt"]),
            regime4H=regime4h,
            structure1H=structure1h,
            session=market_session(confirmed)["name"],
            macroContext=macro_context,
            sourceCandleIds=(*pending["source"], confirmed),
        )
        events.append(event)
        pending = None
    return events


def edge_lifecycle(
    event: dict, profile: dict, *, now: datetime, current_price: float
) -> dict:
    confirmed = pd.Timestamp(event["confirmedAt"])
    if confirmed.tzinfo is None:
        confirmed = confirmed.tz_localize("UTC")
    now_ts = pd.Timestamp(now if now.tzinfo else now.replace(tzinfo=timezone.utc))
    age_min = max(0.0, (now_ts - confirmed).total_seconds() / 60)
    expected = profile.get("expectedMoveAtr")
    sample_size = int(profile.get("sampleSize") or 0)
    bias = str(profile.get("directionalBias") or "NONE")
    if expected and expected > 0 and bias in {"UP", "DOWN"}:
        sign = 1 if bias == "UP" else -1
        realized = max(
            0.0,
            sign
            * (current_price - float(event["referenceMid"]))
            / float(event["referenceAtr"]),
        )
        consumed = min(100.0, 100 * realized / expected)
    else:
        realized, consumed = 0.0, 0.0
    p25 = profile.get("triggerTimeP25Min")
    p75 = profile.get("triggerTimeP75Min")
    if sample_size < 20:
        status = "LOW_CONFIDENCE"
    elif consumed >= 100:
        status = "EXHAUSTED"
    elif p75 is not None and age_min > p75:
        status = "EXPIRED"
    elif consumed >= 75:
        status = "DECAYING"
    elif p25 is not None and age_min < p25:
        status = "FRESH"
    else:
        status = "ACTIVE"
    remaining = max(0.0, 1 - consumed / 100) if sample_size >= 20 else 0.0
    return {
        "doubleSweepAgeMinutes": round(age_min, 2),
        "realizedMoveAtr": round(realized, 3),
        "edgeConsumedPct": round(consumed, 1),
        "doubleSweepEdgeRemaining": round(remaining, 3),
        "edgeStatus": status,
        "statisticalEdgeWeight": round(
            remaining * float(profile.get("confidenceWeight") or 0), 3
        ),
    }

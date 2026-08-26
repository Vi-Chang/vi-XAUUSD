"""User-facing multi-timeframe bias derived from existing runtime assessments.

This module is presentation-only.  It deliberately does not change the
canonical market bias or any entry/risk threshold.
"""
from __future__ import annotations

from typing import Any


def _assessment_rows(source: dict) -> list[dict]:
    normalized = source.get("normalized_analysis") or source
    rows = normalized.get("timeframeAssessments") or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _base_bias(row: dict) -> str:
    trend = str(row.get("trend") or "").upper()
    momentum = str(row.get("momentum") or "").upper()
    label = str(row.get("label") or "").upper()
    combined = f"{trend} {momentum} {label}"
    if not trend and not label:
        return "UNKNOWN"
    if "BULLISH_CORRECTION" in trend:
        return "BULLISH_CORRECTION"
    if "BEARISH_CORRECTION" in trend:
        return "BEARISH_CORRECTION"
    if "TRANSITION" in trend:
        return "TRANSITION"
    if "BULL" in trend or "多頭" in label:
        if any(token in combined for token in ("PULLBACK", "WEAKEN", "REVERSAL_RISK", "回調", "降溫")):
            return "BULLISH_CORRECTION"
        return "BULLISH"
    if "BEAR" in trend or "空頭" in label:
        if any(token in combined for token in ("PULLBACK", "WEAKEN", "REVERSAL_RISK", "反彈", "降溫")):
            return "BEARISH_CORRECTION"
        return "BEARISH"
    if "TRANSITION" in combined or "轉換" in label:
        return "TRANSITION"
    if "NEUTRAL" in trend or "RANGE" in trend or "盤整" in label or "震盪" in label:
        return "NEUTRAL"
    return "UNKNOWN"


def _tactical_vote(value: str) -> float | None:
    return {
        "BULLISH": 1.0,
        "BULLISH_CORRECTION": -0.25,
        "BEARISH": -1.0,
        "BEARISH_CORRECTION": 0.25,
        "NEUTRAL": 0.0,
        "TRANSITION": 0.0,
    }.get(value)


def derive_short_term_bias(snapshot: dict) -> str:
    """Resolve intraday direction in strict 15M -> 1H order.

    4H is deliberately excluded: it is background context, not an intraday
    direction vote. A directional 15M structure cannot be overturned by 1H.
    """
    for field in ("bias15m", "bias1h"):
        vote = _tactical_vote(str(snapshot.get(field) or "UNKNOWN"))
        if vote is None or vote == 0:
            continue
        return "SHORT_TERM_BULLISH" if vote > 0 else "SHORT_TERM_BEARISH"
    return "SHORT_TERM_TRANSITION"


def _macro_bias(snapshot: dict, canonical_bias: str) -> str:
    values = [str(snapshot.get(key) or "UNKNOWN") for key in ("bias1d", "bias4h")]
    bull = sum(value in {"BULLISH", "BULLISH_CORRECTION"} for value in values)
    bear = sum(value in {"BEARISH", "BEARISH_CORRECTION"} for value in values)
    if bull > bear:
        return "MACRO_BULLISH"
    if bear > bull:
        return "MACRO_BEARISH"
    canonical = canonical_bias.upper()
    return ("MACRO_BULLISH" if "BULL" in canonical else
            "MACRO_BEARISH" if "BEAR" in canonical else "MACRO_MIXED")


def _alignment(snapshot: dict) -> str:
    values = [str(snapshot.get(key) or "UNKNOWN") for key in
              ("bias15m", "bias1h", "bias4h", "bias1d")]
    known = [value for value in values if value != "UNKNOWN"]
    if known and all(value == "BULLISH" for value in known):
        return "STRONG_BULL_ALIGNMENT"
    if known and all(value == "BEARISH" for value in known):
        return "STRONG_BEAR_ALIGNMENT"
    short = str(snapshot.get("shortTermBias") or "")
    macro = str(snapshot.get("macroBias") or "")
    if ((short == "SHORT_TERM_BULLISH" and macro == "MACRO_BEARISH") or
            (short == "SHORT_TERM_BEARISH" and macro == "MACRO_BULLISH")):
        return "COUNTERTREND"
    if any(value == "TRANSITION" for value in known):
        return "TRANSITION"
    return "MIXED"


def derive_multi_timeframe_bias(source: dict, *, canonical_bias: str = "NEUTRAL") -> dict[str, Any]:
    rows = {str(row.get("timeframe") or "").upper(): row for row in _assessment_rows(source)}
    snapshot: dict[str, Any] = {
        "bias15m": _base_bias(rows.get("15M", {})),
        "bias1h": _base_bias(rows.get("1H", {})),
        "bias4h": _base_bias(rows.get("4H", {})),
        "bias1d": _base_bias(rows.get("1D", {})),
        "canonicalBias": canonical_bias.upper(),
    }
    snapshot["shortTermBias"] = derive_short_term_bias(snapshot)
    snapshot["macroBias"] = _macro_bias(snapshot, canonical_bias)
    snapshot["alignment"] = _alignment(snapshot)
    snapshot["timeframeAlignmentScore"] = snapshot["alignment"]
    snapshot["hasKnownTimeframes"] = any(
        snapshot[key] != "UNKNOWN" for key in ("bias15m", "bias1h", "bias4h", "bias1d"))
    return snapshot


def timeframe_bias_lines(snapshot: dict) -> list[str]:
    if not snapshot.get("hasKnownTimeframes", any(
            str(snapshot.get(key) or "UNKNOWN") != "UNKNOWN"
            for key in ("bias15m", "bias1h", "bias4h", "bias1d"))):
        return []
    labels = {
        "BULLISH": "🟢 偏多", "BEARISH": "🔴 偏空", "NEUTRAL": "⚪ 震盪",
        "TRANSITION": "🟡 轉換中", "BULLISH_CORRECTION": "🟠 多頭修正／短線偏空",
        "BEARISH_CORRECTION": "🟠 空頭修正／短線偏多",
    }
    lines = []
    for key, timeframe in (("bias15m", "15M"), ("bias1h", "1H"),
                           ("bias4h", "4H"), ("bias1d", "1D")):
        value = str(snapshot.get(key) or "UNKNOWN")
        if value in labels:
            lines.append(f"{timeframe}：{labels[value]}")
    short = {
        "SHORT_TERM_BULLISH": "🟢 偏多", "SHORT_TERM_BEARISH": "🔴 偏空",
        "SHORT_TERM_MIXED": "⚪ 分歧", "SHORT_TERM_TRANSITION": "🟡 轉換中",
    }.get(str(snapshot.get("shortTermBias") or ""))
    macro = {
        "MACRO_BULLISH": "🟢 偏多", "MACRO_BEARISH": "🔴 偏空",
        "MACRO_MIXED": "⚪ 分歧",
    }.get(str(snapshot.get("macroBias") or ""))
    if short:
        lines.append(f"短線：{short}")
    if macro:
        lines.append(f"大方向：{macro}")
    return lines

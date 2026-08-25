"""Additive 15M/1H execution view for the SCALP_INTRADAY horizon."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import get_settings
from app.engines.multi_timeframe_bias import derive_multi_timeframe_bias


def derive_scalp_bias(multi: dict) -> str:
    """15M and 1H decide execution direction; 4H/1D never veto it."""
    m15 = str(multi.get("bias15m") or "UNKNOWN")
    h1 = str(multi.get("bias1h") or "UNKNOWN")

    def side(value: str) -> int | None:
        if value in {"BULLISH", "BEARISH_CORRECTION"}:
            return 1
        if value in {"BEARISH", "BULLISH_CORRECTION"}:
            return -1
        if value in {"NEUTRAL", "TRANSITION"}:
            return 0
        return None

    votes = [vote for vote in (side(m15), side(h1)) if vote is not None]
    if not votes or all(vote == 0 for vote in votes):
        return "SCALP_TRANSITION"
    if len(votes) == 2 and votes[0] * votes[1] < 0:
        return "SCALP_MIXED"
    score = sum(votes) / len(votes)
    if score >= .5:
        return "SCALP_BULLISH"
    if score <= -.5:
        return "SCALP_BEARISH"
    return "SCALP_TRANSITION"


def preferred_scalp_side(scalp_bias: str) -> str:
    return {"SCALP_BULLISH": "LONG", "SCALP_BEARISH": "SHORT",
            "SCALP_MIXED": "BOTH"}.get(scalp_bias, "NONE")


def scalp_setup_ttl_bars(*, atr15: float, price: float) -> int:
    settings = get_settings()
    ratio = atr15 / price if price > 0 else 0.0
    if ratio >= settings.scalp_high_vol_atr_price_ratio:
        return settings.scalp_setup_ttl_bars_high_vol
    if ratio >= settings.scalp_normal_vol_atr_price_ratio:
        return settings.scalp_setup_ttl_bars_normal
    return settings.scalp_setup_ttl_bars_low_vol


def _zone(item: dict) -> dict | None:
    raw = item.get("entry_zone") or item.get("entryZone") or item.get("candidateZone") or {}
    low = raw.get("lower") if raw.get("lower") is not None else raw.get("low")
    high = raw.get("upper") if raw.get("upper") is not None else raw.get("high")
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return {"low": round(min(float(low), float(high)), 2),
            "high": round(max(float(low), float(high)), 2),
            "source": item.get("type") or item.get("zoneRole") or "TACTICAL_STRUCTURE",
            "setupId": item.get("opportunity_id") or item.get("setup_id")}


def _zones(opportunities: list[dict], side: str) -> tuple[dict | None, dict | None]:
    valid = [item for item in opportunities
             if str(item.get("side") or item.get("direction") or "").upper() == side
             and str(item.get("state") or "") not in {"EXPIRED", "INVALIDATED", "REJECTED"}
             and _zone(item)]
    valid.sort(key=lambda item: (
        not bool(item.get("primary_eligible", True)),
        str(item.get("support_role") or "").startswith("SECONDARY"),
        float(item.get("distance_from_current") or item.get("anchor_distance") or 0)))
    primary = _zone(valid[0]) if valid else None
    secondary_item = next((item for item in valid[1:] if
                           str(item.get("support_role") or item.get("anchor_role") or "") in {
                               "SECONDARY_DEEP_SUPPORT", "DEEP_PULLBACK_BACKUP"}), None)
    secondary = _zone(secondary_item) if secondary_item else (
        _zone(valid[1]) if len(valid) > 1 else None)
    return primary, secondary


def _age_bars(created_at: str, now_at: str) -> int:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        current = datetime.fromisoformat(now_at.replace("Z", "+00:00"))
        return max(0, int((current - created).total_seconds() // (15 * 60)))
    except (TypeError, ValueError):
        return 0


def scalp_opportunity_coverage(observation: dict | None) -> dict:
    value = observation or {}
    qualifying = bool(value.get("zoneEntered") and value.get("validReaction") and
                      float(value.get("favorableExcursionR") or 0) >= 1.0)
    recorded = bool(value.get("watchRecorded") or value.get("prepareRecorded") or
                    value.get("entryRecorded"))
    return {"metric": "SCALP_OPPORTUNITY_COVERAGE",
            "coverageGap": qualifying and not recorded,
            "eventType": "SCALP_OPPORTUNITY_COVERAGE_GAP" if qualifying and not recorded else None}


def build_scalp_decision_snapshot(data: dict, canonical: dict | None = None) -> dict[str, Any]:
    canonical = canonical or {}
    normalized = data.get("normalized_analysis") or {}
    multi = canonical.get("multiTimeframeBias") or derive_multi_timeframe_bias(
        normalized, canonical_bias=str(canonical.get("marketBias") or "NEUTRAL"))
    scalp_bias = derive_scalp_bias(multi)
    preferred = preferred_scalp_side(scalp_bias)
    opportunity_engine = data.get("entry_opportunity_engine") or {}
    opportunities = list(opportunity_engine.get("opportunities") or [])
    long_primary, long_secondary = _zones(opportunities, "LONG")
    short_primary, short_secondary = _zones(opportunities, "SHORT")
    price = float(normalized.get("currentPrice") or canonical.get("currentPrice") or 0)
    atr15 = float(normalized.get("atr15") or canonical.get("atr15") or 0)
    ttl = scalp_setup_ttl_bars(atr15=atr15, price=price)
    selected = canonical.get("primarySetup") or {}
    created_at = str(selected.get("createdAt") or selected.get("created_at") or
                     canonical.get("timestamp") or data.get("timestamp_utc") or "")
    now_at = str(data.get("timestamp_utc") or canonical.get("timestamp") or "")
    age = _age_bars(created_at, now_at)
    tactical4h = str(multi.get("bias4h") or "UNKNOWN")
    macro1d = str(multi.get("bias1d") or "UNKNOWN")
    side_word = "BULLISH" if preferred == "LONG" else "BEARISH" if preferred == "SHORT" else ""
    conflict4h = bool(side_word and side_word not in tactical4h)
    conflict1d = bool(side_word and side_word not in macro1d)
    aligned_15_1_4 = bool(side_word and all(side_word in str(multi.get(key) or "")
                                               for key in ("bias15m", "bias1h", "bias4h")))
    targets = list(selected.get("targets") or canonical.get("targets") or [])
    coverage = scalp_opportunity_coverage(data.get("scalp_opportunity_observation"))
    return {
        "schemaVersion": "scalp-decision-v1", "tradingHorizon": "SCALP_INTRADAY",
        "timestamp": now_at, "price": price, "scalpBias": scalp_bias,
        "preferredSide": preferred, "bias15m": multi.get("bias15m"),
        "bias1h": multi.get("bias1h"), "tactical4h": tactical4h,
        "macro1d": macro1d, "primaryLongZone": long_primary,
        "primaryShortZone": short_primary, "secondaryLongZone": long_secondary,
        "secondaryShortZone": short_secondary,
        "opportunityState": canonical.get("setupState") or "WAIT",
        "entryPermission": bool(canonical.get("executionAllowed") or canonical.get("canEnter")),
        "invalidation": selected.get("tacticalStop") or canonical.get("invalidationPrice"),
        "tp1": targets[0] if targets else None, "tp2": targets[1] if len(targets) > 1 else None,
        "setupAge": age, "setupTTL": ttl,
        "setupExpired": bool(age >= ttl), "dataHealth": canonical.get("dataHealth"),
        "counterHigherTimeframe": conflict4h or conflict1d,
        "riskFlags": (["COUNTER_4H_STRUCTURE"] if conflict4h else []) +
                     (["COUNTER_MACRO_TREND_RISK"] if conflict1d else []),
        "managementMode": "ALLOW_RUNNER" if aligned_15_1_4 else
                          "SCALP_ONLY" if conflict4h or conflict1d else "STANDARD_INTRADAY",
        "nextLongScalpOpportunity": long_primary,
        "nextShortScalpOpportunity": short_primary,
        "coverage": coverage,
    }


def scalp_bias_lines(snapshot: dict) -> list[str]:
    bias = str(snapshot.get("scalpBias") or "SCALP_TRANSITION")
    preferred = str(snapshot.get("preferredSide") or "NONE")
    headline = {"SCALP_BULLISH": "短線：🟢 偏多", "SCALP_BEARISH": "短線：🔴 偏空",
                "SCALP_MIXED": "短線：🟡 分歧",
                "SCALP_TRANSITION": "短線：🟡 轉換中"}[bias]
    labels = {"BULLISH": "🟢 偏多", "BEARISH": "🔴 偏空",
              "NEUTRAL": "⚪ 震盪", "TRANSITION": "🟡 轉換中",
              "BULLISH_CORRECTION": "🟠 多頭修正／短線偏空",
              "BEARISH_CORRECTION": "🟠 空頭修正／短線偏多"}
    lines = [headline]
    for key, tf in (("bias15m", "15M"), ("bias1h", "1H"), ("tactical4h", "4H")):
        value = str(snapshot.get(key) or "UNKNOWN")
        if value in labels:
            lines.append(f"{tf}：{labels[value]}")
    lines.append({"LONG": "目前策略：優先找多", "SHORT": "目前策略：優先找空",
                  "BOTH": "目前策略：方向分歧，等確認",
                  "NONE": "目前策略：等待新結構"}[preferred])
    if snapshot.get("counterHigherTimeframe"):
        macro = str(snapshot.get("macro1d") or "")
        if preferred == "SHORT" and "BULLISH" in macro:
            lines.append("⚠️ 日線仍偏多，本空單屬短線回檔交易，採短打。")
        elif preferred == "LONG" and "BEARISH" in macro:
            lines.append("⚠️ 日線仍偏空，本多單屬短線反彈交易，採短打。")
    return lines

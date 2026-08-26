"""Semantic ordering for dynamically-calculated pullback zones."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _bounds(zone: dict) -> tuple[float, float] | None:
    raw = zone.get("entry_zone") or zone.get("entryZone") or zone.get("zone") or {}
    low = raw.get("lower", raw.get("low"))
    high = raw.get("upper", raw.get("high"))
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    first, second = sorted((float(low), float(high)))
    return first, second


def normalize_pullback_zones(side: str, reference_price: float,
                             zones: list[dict]) -> list[dict[str, Any]]:
    """Label zones by anchor distance; the same rule applies to LONG and SHORT."""
    normalized: list[dict[str, Any]] = []
    for original in zones:
        bounds = _bounds(original)
        if bounds is None:
            continue
        low, high = bounds
        item = dict(original)
        item["zoneDistanceFromReference"] = round(abs((low + high) / 2 - reference_price), 4)
        normalized.append(item)
    normalized.sort(key=lambda item: item["zoneDistanceFromReference"])
    for index, item in enumerate(normalized):
        semantic = "SHALLOW" if index == 0 else "DEEP" if index == len(normalized) - 1 else "MEDIUM"
        original_type = str(item.get("type") or "")
        if ((original_type == "SHALLOW_PULLBACK" and semantic != "SHALLOW") or
                (original_type == "DEEP_PULLBACK" and semantic != "DEEP")):
            logger.warning(
                "pullback zone semantic order corrected side=%s reference=%.4f type=%s semantic=%s",
                side, reference_price, original_type, semantic,
            )
        item["semanticPullbackType"] = semantic
    return normalized


def validate_pullback_zone_order(zones: list[dict]) -> bool:
    distances = [float(item["zoneDistanceFromReference"]) for item in zones
                 if isinstance(item.get("zoneDistanceFromReference"), (int, float))]
    return distances == sorted(distances)

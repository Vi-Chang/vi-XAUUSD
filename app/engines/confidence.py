"""Canonical signal confidence grading, independent from trade permission."""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Real

GRADING_VERSION = "signal-score-v1"


def normalize_signal_score(score: object) -> int | None:
    if score is None or isinstance(score, bool) or not isinstance(score, Real):
        return None
    return max(0, min(100, round(float(score))))


def get_confidence_grade(score: object) -> str:
    value = normalize_signal_score(score)
    if value is None:
        return "U"
    if value >= 80:
        return "A"
    if value >= 65:
        return "B"
    if value >= 35:
        return "C"
    return "D"


def confidence_label(score: object) -> str:
    grade = get_confidence_grade(score)
    return {
        "A": "A級（高信心）",
        "B": "B級（中高信心）",
        "C": "C級（中低信心）",
        "D": "D級（低信心）",
        "U": "未評級",
    }[grade]


@dataclass(frozen=True)
class TradePermission:
    trade_status: str
    can_enter: bool
    blocked_reason: str = ""


def permission_from_state(
    state: str, *, existing_status: str = "", existing_reason: str = ""
) -> TradePermission:
    if state in ("LONG_READY", "SHORT_READY"):
        return TradePermission("READY", True, "")
    if state == "MISSED_ENTRY":
        return TradePermission("MISSED_ENTRY", False, existing_reason or "原進場區已錯過")
    if state == "INVALIDATED":
        return TradePermission("INVALIDATED", False, existing_reason or "原劇本已失效")
    if state == "DATA_STALE":
        return TradePermission("BLOCKED_DATA", False, existing_reason or "行情資料過期")
    if state.endswith("MANAGE"):
        return TradePermission("MANAGE", False, existing_reason or "僅管理既有條件式部位")
    if existing_status == "BLOCKED_RR":
        return TradePermission("BLOCKED_RR", False, existing_reason)
    return TradePermission("WAIT_CONFIRMATION", False, existing_reason)

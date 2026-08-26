"""Lifecycle and resolver for closed-candle decision triggers."""
from __future__ import annotations

TERMINAL = {"SATISFIED", "FAILED", "EXPIRED", "SUPERSEDED"}


def _status(condition: str, level: float, closed: float | None) -> str:
    if closed is None:
        return "PENDING"
    satisfied = ((condition == "closeAbove" and closed > level)
                 or (condition == "closeBelow" and closed < level))
    return "SATISFIED" if satisfied else "PENDING"


def resolve_next_trigger(*, resistance: float | None, support: float | None,
                         latest_closed: float | None, direction: str,
                         state: str) -> dict:
    candidates = []
    for condition, level in (("closeAbove", resistance), ("closeBelow", support)):
        if level is not None:
            candidates.append({"condition": condition, "level": level,
                               "timeframe": "15M",
                               "status": _status(condition, level, latest_closed)})
    completed = [item for item in candidates if item["status"] in TERMINAL]
    preferred = "closeBelow" if direction == "SHORT" or state.startswith("SHORT") else "closeAbove"
    pending = next((item for item in candidates
                    if item["condition"] == preferred and item["status"] == "PENDING"), None)
    if pending:
        verb = "站上" if pending["condition"] == "closeAbove" else "跌破"
        label = f"等 15 分鐘收盤{verb} {pending['level']:.2f}"
    elif completed:
        label = "原突破條件已完成，正在等待新結構形成"
    else:
        label = "等待最新市場結構形成新的確認條件"
    return {"triggers": candidates, "completed": completed,
            "next": pending, "label": label}


def validate_notification(event: dict) -> list[str]:
    errors = []
    trigger = event.get("nextTriggerCondition")
    closed = event.get("latestClosedCandlePrice")
    if trigger:
        if trigger.get("status") != "PENDING":
            errors.append("NEXT_TRIGGER_NOT_PENDING")
        level, condition = trigger.get("level"), trigger.get("condition")
        if (isinstance(level, (int, float)) and isinstance(closed, (int, float))
                and ((condition == "closeAbove" and closed > level)
                     or (condition == "closeBelow" and closed < level))):
            errors.append("NEXT_TRIGGER_ALREADY_SATISFIED")
    reasons = " ".join(event.get("transitionReasons") or [])
    if "多方延續" in reasons and trigger and trigger.get("condition") == "closeAbove":
        errors.append("BULLISH_CONTINUATION_WITH_OLD_BREAKOUT_TRIGGER")
    price, short_defense = event.get("currentPrice"), event.get("shortDefensePrice")
    if (isinstance(price, (int, float)) and isinstance(short_defense, (int, float))
            and price > short_defense and "防守條件已觸發" not in str(event.get("shortManage"))):
        errors.append("SHORT_DEFENSE_DISPLAYED_AS_PENDING")
    canonical = event.get("canonicalDecision") or {}
    event_type = str(event.get("event_type") or "")
    if event_type.startswith("DEFENSE_"):
        if event.get("defenseRejected") or canonical.get("defenseRejected"):
            errors.append("STALE_DEFENSE_REJECTED")
        side = str(event.get("defenseSide") or canonical.get("defenseSide") or "")
        if side not in {"LONG", "SHORT"}:
            errors.append("DEFENSE_SIDE_MISSING")
    return errors

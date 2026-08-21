"""Persistent, closed-candle-gated entry opportunity lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone

LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "WAIT_CONFIRMATION": {"WAIT_CONFIRMATION", "CONFIRMED_WAIT_RETEST", "ENTRY_READY", "INVALIDATED"},
    "CONFIRMED_WAIT_RETEST": {"CONFIRMED_WAIT_RETEST", "ENTRY_READY", "INVALIDATED"},
    "ENTRY_READY": {"ENTRY_READY", "MISSED_ENTRY", "INVALIDATED"},
    "MISSED_ENTRY": {"MISSED_ENTRY", "INVALIDATED"},
    "INVALIDATED": {"INVALIDATED"},
}


def _now(value: str) -> str:
    return value or datetime.now(timezone.utc).isoformat()


def evaluate_setup_lifecycle(
    *, previous: dict | None, setup_id: str, direction: str,
    confirmation_price: float | None, latest_closed_price: float | None,
    closed_candle_time: str, current_price: float,
    entry_zone_low: float | None, entry_zone_high: float | None,
    risk_controls_passed: bool, calculated_at: str,
    invalidated: bool = False,
) -> dict:
    """Resolve lifecycle in mandatory confirmation→ready→missed order."""
    previous = previous or {}
    same_setup = bool(setup_id and previous.get("setupId") == setup_id)
    old = str(previous.get("state") or "WAIT_CONFIRMATION") if same_setup else "WAIT_CONFIRMATION"
    confirmed_at = previous.get("confirmedAt") if same_setup else None
    confirmed_candle = previous.get("confirmedCandleTime") if same_setup else None
    ready_at = previous.get("entryReadyAt") if same_setup else None
    notified_at = previous.get("entryNotificationSentAt") if same_setup else None
    was_ready = bool(previous.get("wasEntryReady")) if same_setup else False
    missed_at = previous.get("missedAt") if same_setup else None

    if invalidated:
        desired, reason = "INVALIDATED", "原交易劇本已失效"
    else:
        confirmed = confirmation_price is None or (
            latest_closed_price is not None
            and ((direction == "LONG" and latest_closed_price > confirmation_price)
                 or (direction == "SHORT" and latest_closed_price < confirmation_price))
        )
        in_zone = (
            entry_zone_low is not None and entry_zone_high is not None
            and entry_zone_low <= current_price <= entry_zone_high
        )
        if not confirmed:
            desired, reason = "WAIT_CONFIRMATION", "最新已收盤 15M 尚未完成方向確認"
        else:
            confirmed_at = confirmed_at or _now(calculated_at)
            confirmed_candle = confirmed_candle or closed_candle_time
            # MISSED_ENTRY is terminal for this setup and is evaluated only after
            # an executable opportunity was both reached and actually notified.
            if was_ready and notified_at and not in_zone:
                desired, reason = "MISSED_ENTRY", "已通知的進場機會其後離開允許進場區"
                missed_at = missed_at or _now(calculated_at)
            elif in_zone and risk_controls_passed:
                desired, reason = "ENTRY_READY", "收盤確認、進場區與風控條件均已通過"
                was_ready = True
                ready_at = ready_at or _now(calculated_at)
            else:
                desired, reason = "CONFIRMED_WAIT_RETEST", (
                    "突破確認完成，但目前價格不在合理進場區，等待回踩"
                    if not in_zone else "突破確認完成，但風控條件尚未通過"
                )

    if desired not in LEGAL_TRANSITIONS.get(old, {old}):
        rejected = desired
        desired = old
        reason = f"拒絕非法狀態轉換 {old} → {rejected}"
    return {
        "setupId": setup_id,
        "state": desired,
        "direction": direction,
        "confirmationRequired": confirmation_price is not None,
        "confirmationPrice": confirmation_price,
        "confirmedAt": confirmed_at,
        "confirmedCandleTime": confirmed_candle,
        "entryZoneLow": entry_zone_low,
        "entryZoneHigh": entry_zone_high,
        "entryReadyAt": ready_at,
        "entryNotificationSentAt": notified_at,
        "wasEntryReady": was_ready,
        "missedAt": missed_at,
        "stateReason": reason,
    }


def mark_entry_notification_sent(lifecycle: dict, *, sent_at: str) -> dict:
    if lifecycle.get("state") != "ENTRY_READY":
        return lifecycle
    return {**lifecycle, "entryNotificationSentAt": sent_at, "wasEntryReady": True}

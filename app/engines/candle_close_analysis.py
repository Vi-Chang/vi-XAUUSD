"""Finalized 15M/1H analysis reports derived from the canonical decision."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pandas as pd

from app.config import get_settings

REPORT_EVENTS = {
    "15M": "CANDLE_CLOSE_ANALYSIS_15M",
    "1H": "CANDLE_CLOSE_ANALYSIS_1H",
    "COMBINED": "CANDLE_CLOSE_ANALYSIS_COMBINED",
}


def _time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _number(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _latest(frame: pd.DataFrame | None, closed: dict) -> dict:
    if frame is None or frame.empty or not closed.get("available"):
        return {"available": False, "closeTime": str(closed.get("close_time") or "")}
    row = frame.iloc[-1]
    return {
        "available": True, "open": _number(row.get("open")),
        "high": _number(row.get("high")), "low": _number(row.get("low")),
        "close": _number(row.get("close")), "volume": _number(row.get("volume")),
        "closeTime": str(closed.get("close_time") or ""),
        "source": closed.get("source"),
    }


def _side(value) -> str:
    text = str(value or "").upper()
    if any(word in text for word in ("LONG", "BULL")):
        return "LONG"
    if any(word in text for word in ("SHORT", "BEAR")):
        return "SHORT"
    return "NEUTRAL"


def _key(symbol: str, report_type: str, close_time: str) -> str:
    return f"{symbol}:{report_type}:{close_time}:CLOSE_ANALYSIS"


def _levels(data: dict, volume: dict) -> tuple[float | None, float | None]:
    normalized = data.get("normalized_analysis") or {}
    levels = list(normalized.get("confirmationLevels") or [])
    current = _number(normalized.get("currentPrice")) or 0.0
    prices: list[float] = []
    for item in levels:
        if isinstance(item, dict):
            for field in ("level", "price", "low", "high"):
                value = _number(item.get(field))
                if value is not None:
                    prices.append(value)
    m15 = (volume.get("timeframes") or {}).get("15M") or {}
    for field in ("priorSupport", "priorResistance"):
        value = _number(m15.get(field))
        if value is not None:
            prices.append(value)
    support = max((price for price in prices if price <= current), default=None)
    resistance = min((price for price in prices if price >= current), default=None)
    return support, resistance


def _what_changed(previous: dict, current: dict) -> list[str]:
    changed: list[str] = []
    old_bias, new_bias = previous.get("executionBias"), current.get("executionBias")
    if old_bias and old_bias != new_bias:
        side_text = {"LONG": "偏多", "SHORT": "偏空", "NEUTRAL": "中立"}
        changed.append(
            f"操作方向由 {side_text.get(str(old_bias), '中立')} "
            f"變成 {side_text.get(str(new_bias), '中立')}")
    old_volume, new_volume = previous.get("volumePriceState"), current.get(
        "volumePriceState")
    if old_volume and old_volume != new_volume:
        changed.append("本根量價狀態出現變化")
    old_quality, new_quality = previous.get("breakoutQuality"), current.get(
        "breakoutQuality")
    if old_quality and old_quality != new_quality:
        changed.append("突破可信度已重新評估")
    if not changed:
        changed.append("這根 K 棒沒有改變主要結構")
    return changed[:3]


def _build_report(*, symbol: str, report_type: str, close_time: str,
                  candles: dict, data: dict, decision: dict, volume: dict,
                  previous_report: dict) -> dict:
    canonical = decision.get("canonicalDecision") or {}
    gate = decision.get("entryOpportunityGate") or {}
    volume_tfs = volume.get("timeframes") or {}
    normalized = data.get("normalized_analysis") or {}
    execution = str(decision.get("executionBias") or canonical.get(
        "executionBias") or "NEUTRAL")
    structural = str(decision.get("structuralBias") or canonical.get(
        "structuralBias") or decision.get("marketBias") or "NEUTRAL")
    data_health = str(decision.get("dataHealth") or canonical.get(
        "dataHealth") or normalized.get("marketDataStatus") or "STALE")
    support, resistance = _levels(data, volume)
    selected = gate.get("selected") or {}
    long_score = int(gate.get("longScore") or 0)
    short_score = int(gate.get("shortScore") or 0)
    action = str(decision.get("finalAction") or "WAIT")
    can_enter = bool(decision.get("canEnter"))
    event = data.get("event_risk") or {}
    blackout = bool(event.get("event_lockout") or event.get("post_event_wait"))
    critical_data = data_health in {"STALE", "FAILED", "DISCONNECTED"}
    if critical_data or blackout:
        can_enter = False
        action = "BLOCKED_DATA" if critical_data else "BLOCKED_EVENT"
    primary_tf = "1H" if report_type == "1H" else "15M"
    primary_volume = volume_tfs.get(primary_tf) or volume_tfs.get("15M") or {}
    report = {
        "reportType": report_type, "closeTime": close_time,
        "candles": candles, "structuralBias": structural,
        "liveBiasState": str(decision.get("liveBiasState") or "ALIGNED"),
        "executionBias": execution, "marketDirection": _side(execution),
        "currentPrice": _number(normalized.get("currentPrice")),
        "dataHealth": data_health, "eventBlackout": blackout,
        "canEnter": can_enter, "currentAction": action,
        "entryState": str(gate.get("entryState") or decision.get("state") or "WATCH"),
        "longScore": long_score, "shortScore": short_score,
        "support": support, "resistance": resistance,
        "entryZone": decision.get("entryZone") or selected.get("entryZone"),
        "stopLoss": decision.get("stopLoss") or selected.get("invalidationPrice"),
        "targets": list(decision.get("targets") or []),
        "canonicalNextTrigger": canonical.get("canonicalNextTrigger"),
        "volume": volume_tfs, "volumePriceState": primary_volume.get(
            "volumePriceState") or "UNAVAILABLE",
        "breakoutQuality": primary_volume.get("breakoutQuality") or "UNAVAILABLE",
        "volumeReason": list(primary_volume.get("reasons") or []),
        "session": volume.get("session") or "UNKNOWN",
    }
    report["whatChanged"] = _what_changed(previous_report, report)
    report["nextFocus"] = (
        "等待行情資料恢復，不使用本根產生新進場" if critical_data else
        "重大事件期間只觀察結構，不建立新倉" if blackout else
        "觀察突破後能否守住，避免直接追價" if resistance is not None and
        report["marketDirection"] == "LONG" else
        "觀察跌破後能否延續，避免直接追空" if support is not None and
        report["marketDirection"] == "SHORT" else
        "等待支撐或壓力被收盤確認後再行動")
    return report


def evaluate_candle_close_reports(
    data: dict, decision: dict, *, volume: dict,
    m15_closed: pd.DataFrame | None, h1_closed: pd.DataFrame | None,
    previous: dict | None = None, evaluated_at: str | None = None,
) -> tuple[dict, list[dict]]:
    """Emit exactly one auditable report for every newly finalized candle."""
    previous = dict(previous or {})
    now = _time(evaluated_at) or datetime.now(timezone.utc)
    symbol = str(data.get("symbol") or "XAUUSD")
    closed = data.get("closed_candles") or {}
    m15 = _latest(m15_closed, dict(closed.get("15M") or {}))
    h1 = _latest(h1_closed, dict(closed.get("1H") or {}))
    m15_time, h1_time = str(m15.get("closeTime") or ""), str(h1.get("closeTime") or "")
    new15 = bool(m15.get("available") and m15_time and
                 m15_time != previous.get("last15mReportTime"))
    # On bootstrap the latest older 1H bar is context, not a missed event to
    # emit on the next scheduler poll.  A matching top-of-hour pair is still
    # combined immediately.
    new1h = bool(h1.get("available") and h1_time and (
        (previous and h1_time != previous.get("last1hReportTime")) or
        (new15 and h1_time == m15_time)))
    pending = dict(previous.get("pendingCombined") or {})
    report_type = close_time = ""
    candles: dict = {}
    parsed_m15_time = _time(m15_time)
    top_of_hour = bool(parsed_m15_time and parsed_m15_time.minute == 0)
    if new15 and new1h and m15_time == h1_time:
        report_type, close_time = "COMBINED", m15_time
        candles = {"15M": m15, "1H": h1}
        pending = {}
    elif new15 and top_of_hour and h1_time != m15_time:
        first_seen = (_time(pending.get("firstSeen")) or now) if pending.get(
            "closeTime") == m15_time else now
        elapsed = (now-first_seen).total_seconds()
        if elapsed < get_settings().combined_close_report_wait_seconds:
            pending = {"closeTime": m15_time, "firstSeen": first_seen.isoformat()}
        else:
            report_type, close_time, candles = "15M", m15_time, {"15M": m15}
            pending = {}
    elif new15:
        report_type, close_time, candles = "15M", m15_time, {"15M": m15}
    elif new1h:
        report_type, close_time, candles = "1H", h1_time, {"1H": h1, "15M": m15}
    elif pending and h1_time == str(pending.get("closeTime") or ""):
        report_type, close_time = "COMBINED", h1_time
        candles, pending = {"15M": m15, "1H": h1}, {}
    state = dict(previous)
    state["pendingCombined"] = pending
    if not previous and h1_time:
        state["last1hReportTime"] = h1_time
    if not report_type:
        return state, []
    prior_report = dict(previous.get("lastReport") or {})
    report = _build_report(
        symbol=symbol, report_type=report_type, close_time=close_time,
        candles=candles, data=data, decision=decision, volume=volume,
        previous_report=prior_report)
    event_type = REPORT_EVENTS[report_type]
    dedupe_key = _key(symbol, report_type, close_time)
    event_id = "CCR-" + hashlib.sha256(dedupe_key.encode()).hexdigest()[:28]
    event = {
        "eventId": event_id, "eventVersion": 1, "event_type": event_type,
        "eventKey": dedupe_key, "reportDedupeKey": dedupe_key,
        "reportType": report_type, "timeframe": report_type,
        "candleCloseTime": close_time, "decisionBasisCandleCloseTime": close_time,
        "currentState": str(decision.get("state") or "WAIT"),
        "finalDecision": str(decision.get("finalAction") or "WAIT"),
        "canEnter": report["canEnter"], "currentPrice": report["currentPrice"],
        "marketBias": decision.get("marketBias"),
        "structuralBias": report["structuralBias"],
        "liveBiasState": report["liveBiasState"],
        "executionBias": report["executionBias"],
        "entryConfirmation": decision.get("entryConfirmation"),
        "dataHealth": report["dataHealth"],
        "entryZone": report["entryZone"], "stopLoss": report["stopLoss"],
        "targets": report["targets"], "candleCloseReport": report,
        "transitionReason": "已取得新的正式收盤 K 棒，依最新資料完整重算",
        "calculatedAt": now.isoformat(), "generatedAtUtc": now.isoformat(),
        "evaluationCycleId": str(decision.get("decisionId") or close_time),
        "decisionId": decision.get("decisionId"),
        "decisionVersion": decision.get("decisionVersion"),
        "canonicalStateVersion": decision.get("decisionVersion"),
        "dataVersion": int(data.get("version") or 0),
        "canonicalDecision": decision.get("canonicalDecision") or {},
    }
    if report_type in {"15M", "COMBINED"}:
        state["last15mReportTime"] = m15_time
    if report_type in {"1H", "COMBINED"}:
        state["last1hReportTime"] = h1_time
    state["lastReport"] = report
    history = list(state.get("history") or [])
    history.append({**report, "telegramReportKey": dedupe_key,
                    "telegramSent": None, "recordedAt": now.isoformat()})
    state["history"] = history[-get_settings().candle_close_report_history_limit:]
    return state, [event]

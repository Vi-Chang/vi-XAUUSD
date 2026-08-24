"""Canonical freshness state shared by strategy, API, UI and Telegram."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from app.config import get_settings
from app.services.market_calendar import market_is_open
from app.utils.timeutils import iso_utc, parse_utc

FreshnessStatus = Literal["fresh", "degraded", "stale"]
HEALTH_STATES = {"FRESH", "DEGRADED", "STALE", "RECOVERING",
                 "DISCONNECTED", "MARKET_CLOSED"}


def _item(value: str | datetime | None, *, now: datetime, fresh_seconds: int,
          degraded_seconds: int, missing_reason: str) -> dict:
    parsed = parse_utc(value)
    if parsed is None:
        return {"status": "stale", "lastUpdatedAtUtc": "",
                "ageSeconds": None, "reason": missing_reason}
    age = max(0.0, (now - parsed).total_seconds())
    if age <= fresh_seconds:
        status: FreshnessStatus = "fresh"
        reason = "資料時間正常"
    elif age <= degraded_seconds:
        status = "degraded"
        reason = f"資料已延遲 {age:.0f} 秒"
    else:
        status = "stale"
        reason = f"資料已過期 {age:.0f} 秒"
    return {"status": status, "lastUpdatedAtUtc": iso_utc(parsed),
            "ageSeconds": round(age, 3), "reason": reason}


def evaluate_freshness_state(data: dict, *, now: datetime | None = None,
                             previous_health_state: str | None = None) -> dict:
    now = parse_utc(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    settings = get_settings()
    normalized = data.get("normalized_analysis") or {}
    quote = data.get("current_price") or {}
    event = data.get("event_risk") or {}
    quote_stamp = quote.get("last_update") or normalized.get("marketDataTimestamp")
    candle_stamp = normalized.get("lastClosedCandleTimestamp")
    event_stamp = event.get("data_updated_at") or normalized.get("eventDataTimestamp")
    calendar = data.get("calendar_data") or {}
    calendar_stamp = calendar.get("last_updated_at") or event_stamp
    market_open = market_is_open(now)
    quote_limit = max(settings.stale_price_seconds, settings.tier1_quote_seconds * 3)
    market = _item(quote_stamp, now=now, fresh_seconds=quote_limit,
                   degraded_seconds=quote_limit * 3,
                   missing_reason="缺少即時行情時間")
    candle = _item(candle_stamp, now=now, fresh_seconds=20 * 60,
                   degraded_seconds=35 * 60,
                   missing_reason="缺少最新已收盤 15M 時間")
    if not market_open:
        # Closed-market data is not actionable, but it is not a timezone error.
        market["reason"] = "目前休市，保留最後行情供參考"
        candle["reason"] = "目前休市，保留最後已收盤 K 棒供參考"
    severity = {"fresh": 0, "degraded": 1, "stale": 2}
    market_status: FreshnessStatus = (
        market["status"] if severity[market["status"]] >= severity[candle["status"]]
        else candle["status"]
    )
    market_combined = {
        "status": market_status,
        "lastUpdatedAtUtc": market["lastUpdatedAtUtc"],
        "ageSeconds": market["ageSeconds"],
        "reason": market["reason"] if market_status == market["status"] else candle["reason"],
        "latestClosedCandleAtUtc": candle["lastUpdatedAtUtc"],
        "closedCandleAgeSeconds": candle["ageSeconds"],
    }
    if not market_open:
        health_state = "MARKET_CLOSED"
    elif not market["lastUpdatedAtUtc"]:
        health_state = "DISCONNECTED"
    elif market_status == "stale":
        health_state = "STALE"
    elif previous_health_state in {"STALE", "DISCONNECTED", "MARKET_CLOSED"}:
        health_state = "RECOVERING"
    elif market_status == "degraded":
        health_state = "DEGRADED"
    else:
        health_state = "FRESH"
    market_combined["healthState"] = health_state
    events = _item(event_stamp, now=now, fresh_seconds=24 * 3600,
                   degraded_seconds=7 * 24 * 3600,
                   missing_reason="事件資料時間缺失")
    calendar = _item(calendar_stamp, now=now, fresh_seconds=24 * 3600,
                     degraded_seconds=7 * 24 * 3600,
                     missing_reason="交易日曆時間缺失")
    strategy_status: FreshnessStatus = (
        "stale" if market_status == "stale"
        else "degraded" if market_status == "degraded" or events["status"] != "fresh"
        else "fresh"
    )
    strategy = {
        "status": strategy_status,
        "lastUpdatedAtUtc": iso_utc(data.get("timestamp_utc")),
        "ageSeconds": _item(data.get("timestamp_utc"), now=now,
                            fresh_seconds=15 * 60, degraded_seconds=30 * 60,
                            missing_reason="分析時間缺失")["ageSeconds"],
        "reason": ("行情過期，禁止產生新進場訊號" if strategy_status == "stale"
                   else "部分輔助資料降級，策略降低可信度" if strategy_status == "degraded"
                   else "策略所需資料正常"),
    }
    return {"marketFreshness": market_combined, "eventFreshness": events,
            "calendarFreshness": calendar, "strategyFreshness": strategy,
            "evaluatedAtUtc": iso_utc(now), "marketOpen": market_open,
            "healthState": health_state}

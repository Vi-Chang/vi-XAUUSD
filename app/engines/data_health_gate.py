"""Fail-closed market-data health gate used by every presentation channel."""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings


def evaluate_data_health(data: dict) -> dict:
    normalized = data.get("normalized_analysis") or {}
    price = data.get("current_price") or {}
    current = price.get("mid")
    market_status = str(normalized.get("marketDataStatus") or
                        (data.get("data_quality") or {}).get("status") or "FAILED").upper()
    candle_time = normalized.get("marketDataTimestamp") or data.get("snapshot_ts") or ""
    reasons: list[str] = []
    status = "HEALTHY"
    now = datetime.now(timezone.utc)
    quote_time = str(price.get("last_update") or "")
    data_age_seconds: float | None = None
    try:
        parsed = datetime.fromisoformat(quote_time.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        data_age_seconds = max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())
    except ValueError:
        pass
    if current is None or float(current) <= 0:
        status, reasons = "INVALID_PRICE", ["即時價格缺失或無效"]
    elif not candle_time:
        status, reasons = "MISSING_CANDLE", ["缺少最新已收盤 K 線時間"]
    elif market_status in {"FAILED", "ERROR", "INSUFFICIENT"}:
        status, reasons = "MISSING_CANDLE", ["行情資料不足"]
    elif market_status in {"STALE", "DEGRADED"}:
        status, reasons = "STALE", ["行情或已收盤 K 線已過期"]
    elif (str(price.get("provider") or "").lower() in {"capital_com", "twelve_data", "finnhub"}
          and data_age_seconds is not None
          and data_age_seconds > get_settings().stale_price_seconds):
        status, reasons = "STALE", [f"即時報價已延遲 {data_age_seconds:.0f} 秒"]
    quality = data.get("data_quality") or {}
    if quality.get("source_mismatch") is True:
        status, reasons = "SOURCE_DIVERGENCE", ["行情來源價格差異超出允許範圍"]
    structural_checks = {
        "candle_complete": "最新K棒資料不完整",
        "timeframe_aligned": "多週期K棒時間未對齊",
        "timezone_consistent": "行情時區不一致",
        "duplicate_free": "偵測到重複K棒",
    }
    for key, message in structural_checks.items():
        if quality.get(key) is False:
            status, reasons = "INVALID_CANDLE_SET", [message]
            break
    closed = normalized.get("lastClosedCandlePrice")
    atr = normalized.get("atr15")
    if (status == "HEALTHY" and isinstance(current, (int, float))
            and isinstance(closed, (int, float)) and isinstance(atr, (int, float))
            and float(atr) > 0):
        settings = get_settings()
        gap = abs(float(current) - float(closed))
        limit = max(settings.quote_candle_divergence_min_abs,
                    float(atr) * settings.quote_candle_divergence_atr_mult)
        if gap > limit:
            status = "QUOTE_CANDLE_DIVERGENCE"
            reasons = [
                f"即時報價與最新已收盤15M相差 {gap:.2f}，超過同步門檻 {limit:.2f}；等待下一根K棒更新，暫停新進場"
            ]
    return {
        "status": status, "healthy": status == "HEALTHY", "reasons": reasons,
        "marketDataTimestamp": candle_time, "quoteTime": quote_time,
        "dataAgeSeconds": round(data_age_seconds, 3) if data_age_seconds is not None else None,
        "currentPrice": current, "provider": price.get("provider") or "",
        "lastClosedCandlePrice": closed,
        "evaluatedAt": now.isoformat(),
    }

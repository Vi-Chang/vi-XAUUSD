"""User-facing HEALTHY/DEGRADED market-data transitions."""
from __future__ import annotations


def _direction(payload: dict) -> str:
    from app.services.current_decision_store import get_canonical_market_bias
    value = get_canonical_market_bias(str(payload.get("symbol") or "XAUUSD"))
    return {"BULLISH": "🟢 偏多", "BEARISH": "🔴 偏空",
            "NEUTRAL": "⚪ 震盪"}.get(value, "⚪ 暫無法確認")


def _level(payload: dict, kind: str) -> str:
    levels = [item.get("price") for item in
              (payload.get("normalized_analysis") or {}).get("confirmationLevels") or []
              if item.get("kind") == kind and isinstance(item.get("price"), (int, float))]
    if not levels:
        return "—"
    current = float((payload.get("normalized_analysis") or {}).get("currentPrice") or 0)
    eligible = ([value for value in levels if value <= current] if kind == "support"
                else [value for value in levels if value >= current])
    chosen = (max(eligible) if kind == "support" and eligible else
              min(eligible) if kind == "resistance" and eligible else levels[0])
    return f"{float(chosen):.2f}"


async def notify_market_data_transition(*, notifier, previous: str | None,
                                        health: dict, payload: dict) -> str:
    current = str(health.get("status") or "GOOD")
    if not notifier or current == previous:
        return current
    provider = str(health.get("provider") or "market-data")
    normalized = payload.get("normalized_analysis") or {}
    last_time = str(normalized.get("lastClosedCandleTimestamp") or
                    normalized.get("marketDataTimestamp") or "尚無")
    if current == "DEGRADED":
        from app.services.market_data_metrics import metrics
        metrics.counters["telegram_error_notifications_total"] += 1
        message = "\n".join([
            "⚠️【行情資料暫時延遲】",
            f"XAUUSD 行情供應商 {provider} 目前受到流量限制或同步異常。",
            f"最後有效資料：{last_time}",
            f"最後市場方向：{_direction(payload)}",
            "目前：⏸ 新進場訊號暫停確認",
            f"既有關鍵位：支撐 {_level(payload, 'support')}｜壓力 {_level(payload, 'resistance')}",
            "系統正在使用最後有效資料並自動恢復同步。",
        ])
        await notifier.notify(
            "RISK", f"market-data-degraded:{provider}:XAUUSD", message,
            severity="WARN", persistent_cooldown_seconds=600)
    elif previous == "DEGRADED" and current == "GOOD":
        final = payload.get("final_decision_state") or {}
        trigger = (final.get("nextAction") or {}).get("triggerLevel")
        next_text = f"{float(trigger):.2f}" if isinstance(trigger, (int, float)) else "等待最新結構"
        message = "\n".join([
            "🟢【XAUUSD 行情同步已恢復】",
            "資料來源已恢復正常。",
            "✓ 即時價格  ✓ 15M  ✓ 1H  ✓ 4H",
            "✓ 關鍵位  ✓ 市場結構",
            f"目前方向：{_direction(payload)}",
            f"下一觸發：{next_text}",
            f"資料時間：{last_time}",
        ])
        await notifier.notify(
            "INFO", f"market-data-recovered:{provider}:{last_time}", message,
            force_push=True, exact_once=True)
    return current

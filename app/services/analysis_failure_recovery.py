"""Retry, degraded-mode and last-known-good recovery for full analysis."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import MarketMonitorState
from app.db.session import db_session
from app.engines.market_direction import resolve_market_direction

logger = logging.getLogger(__name__)
LKG_MONITOR_KEY = "last_known_good_analysis"


def _levels(data: dict, kind: str) -> list[float]:
    normalized = data.get("normalized_analysis") or {}
    return [float(item["price"]) for item in normalized.get("confirmationLevels") or []
            if item.get("kind") == kind
            and isinstance(item.get("price"), (int, float))]


def build_last_known_good_state(data: dict) -> dict:
    symbol = str(data.get("symbol") or "XAUUSD")
    final = data.get("final_decision_state") or {}
    normalized = data.get("normalized_analysis") or {}
    direction = resolve_market_direction(data, final)
    supports, resistances = _levels(data, "support"), _levels(data, "resistance")
    return {
        "schemaVersion": "last-known-good-v1", "symbol": symbol,
        "direction": direction["direction"], "directionSource": direction["source"],
        "structure": {
            "marketRegime": normalized.get("marketRegime"),
            "trendBias": normalized.get("trendBias"),
            "breakoutState": normalized.get("breakoutState"),
            "supportState": normalized.get("supportState"),
        },
        "keySupport": max(supports, default=None),
        "keyResistance": min(resistances, default=None),
        "activeSetup": final.get("selectedScenarioId") or "",
        "invalidation": final.get("invalidationPrice"),
        "nextTrigger": final.get("nextAction") or final.get("nextTriggerCondition") or {},
        "lastClosedCandle": {
            "timestamp": normalized.get("lastClosedCandleTimestamp"),
            "price": normalized.get("lastClosedCandlePrice"),
        },
        "confidence": final.get("qualityScore") or final.get("rawScore"),
        "timestamp": str(data.get("timestamp_utc") or data.get("snapshot_ts") or
                         datetime.now(timezone.utc).isoformat()),
    }


def persist_last_known_good(data: dict) -> dict:
    state = build_last_known_good_state(data)
    now = datetime.now(timezone.utc)
    with db_session() as db:
        row = db.execute(select(MarketMonitorState).where(
            MarketMonitorState.symbol == state["symbol"],
            MarketMonitorState.monitor_key == LKG_MONITOR_KEY,
        )).scalar_one_or_none()
        if row is None:
            row = MarketMonitorState(symbol=state["symbol"], monitor_key=LKG_MONITOR_KEY,
                                     updated_at=now)
            db.add(row)
        row.payload, row.updated_at = state, now
    return state


def load_last_known_good(symbol: str = "XAUUSD") -> dict:
    with db_session() as db:
        row = db.execute(select(MarketMonitorState).where(
            MarketMonitorState.symbol == symbol,
            MarketMonitorState.monitor_key == LKG_MONITOR_KEY,
        )).scalar_one_or_none()
        return dict(row.payload or {}) if row else {}


def build_degraded_result(current: dict | None, last_good: dict, *, module: str) -> dict:
    output = copy.deepcopy(current or {})
    normalized = dict(output.get("normalized_analysis") or {})
    structure = last_good.get("structure") or {}
    for key in ("marketRegime", "trendBias", "breakoutState", "supportState"):
        if normalized.get(key) in (None, "", "UNKNOWN", "unknown") and structure.get(key):
            normalized[key] = structure[key]
    normalized["marketDataStatus"] = "STALE"
    normalized["entryReadiness"] = "no_trade"
    normalized["longEntryAllowed"] = False
    normalized["shortEntryAllowed"] = False
    output["normalized_analysis"] = normalized
    direction = str(last_good.get("direction") or "UNKNOWN")
    output["analysis_failure_recovery"] = {
        "status": "DEGRADED", "module": module,
        "lastKnownGood": last_good, "entrySignalsPaused": True,
    }
    output["last_known_good_state"] = last_good
    final = dict(output.get("final_decision_state") or {})
    final.update({
        "finalAction": "NO_TRADE", "canEnter": False,
        "state": "DATA_STALE", "entrySignal": "PAUSED",
        "marketDirection": direction,
        "humanSummary": "分析服務暫時異常；沿用上一份有效方向並暫停更新交易訊號。",
        "primaryReason": "ANALYSIS_DEGRADED",
    })
    output["final_decision_state"] = final
    return output


@dataclass
class AnalysisFailureRecovery:
    degraded: bool = False
    episode_id: str = ""
    degraded_notice_sent: bool = False
    last_error_fingerprint: str = ""

    async def execute(
        self, operation: Callable[[], Awaitable], *, notifier=None,
        current: dict | None = None, symbol: str = "XAUUSD",
        module: str = "full_analysis", sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ) -> tuple[object | None, dict | None]:
        delays = tuple(get_settings().analysis_retry_delays_seconds)
        last_error: Exception | None = None
        for attempt in range(len(delays) + 1):
            try:
                result = await operation()
                payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
                try:
                    persist_last_known_good(payload)
                except Exception:
                    logger.exception("failed to persist last-known-good analysis")
                if self.degraded:
                    await self._notify_recovered(notifier, payload, symbol)
                self.degraded = False
                self.degraded_notice_sent = False
                self.last_error_fingerprint = ""
                return result, None
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("analysis attempt %d/%d failed in %s: %s",
                               attempt + 1, len(delays) + 1, module, type(exc).__name__)
                if attempt < len(delays):
                    await sleep(float(delays[attempt]))
        assert last_error is not None
        degraded = await self.report_failure(
            last_error, notifier=notifier, current=current,
            symbol=symbol, module=module)
        return None, degraded

    async def report_failure(self, exc: Exception, *, notifier=None,
                             current: dict | None = None, symbol: str = "XAUUSD",
                             module: str = "full_analysis") -> dict:
        fingerprint = f"{type(exc).__name__}:{module}:{symbol}"
        if not self.degraded:
            self.episode_id = hashlib.sha256(
                f"{fingerprint}:{datetime.now(timezone.utc).isoformat()}".encode()
            ).hexdigest()[:16]
        self.degraded = True
        self.last_error_fingerprint = fingerprint
        try:
            last_good = load_last_known_good(symbol)
        except Exception:
            logger.exception("failed to load last-known-good analysis")
            last_good = build_last_known_good_state(current or {})
        degraded = build_degraded_result(current, last_good, module=module)
        if notifier and not self.degraded_notice_sent:
            direction = {"BULLISH": "偏多", "BEARISH": "偏空",
                         "NEUTRAL": "中立"}.get(
                             str(last_good.get("direction") or "UNKNOWN"), "暫無法確認")
            message = (
                "⚠️【分析服務暫時異常】\n\n"
                "目前報價仍由報價層持續監控\n"
                f"最後有效市場方向：{direction}\n"
                f"最後有效資料時間：{last_good.get('timestamp') or '尚無'}\n"
                "系統正在自動恢復\n"
                "交易訊號暫停更新"
            )
            await notifier.notify(
                "RISK", f"analysis-degraded:{fingerprint}", message,
                severity="WARN",
                persistent_cooldown_seconds=get_settings().analysis_error_cooldown_seconds)
            self.degraded_notice_sent = True
        return degraded

    async def _notify_recovered(self, notifier, payload: dict, symbol: str) -> None:
        if not notifier:
            return
        final = payload.get("final_decision_state") or {}
        direction = {"BULLISH": "偏多", "BEARISH": "偏空", "NEUTRAL": "中立"}.get(
            str(final.get("marketDirection") or
                resolve_market_direction(payload, final)["direction"]), "暫無法確認")
        next_action = final.get("nextAction") or {}
        trigger = next_action.get("triggerLevel")
        next_text = f"15M 收盤確認 {float(trigger):.2f}" if isinstance(
            trigger, (int, float)) else "等待新的市場結構"
        message = (
            "🟢【分析服務已恢復】\n\n"
            f"{symbol} 市場分析已恢復正常\n"
            "已重新同步：行情、K線、關鍵位、市場結構與進場條件\n"
            f"目前方向：{direction}\n"
            f"下一觸發：{next_text}"
        )
        await notifier.notify(
            "INFO", f"analysis-recovered:{self.episode_id}", message,
            force_push=True, exact_once=True)

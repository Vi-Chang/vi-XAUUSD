"""Low-cardinality market-data reliability metrics and request audit trail."""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone


class MarketDataMetrics:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.by_timeframe: Counter[str] = Counter()
        self.requests: deque[dict] = deque(maxlen=250)
        self.circuit_state = "CLOSED"
        self.market_data_age_seconds: float | None = None

    def record_request(self, *, provider: str, symbol: str, timeframe: str,
                       caller: str, reason: str, cache_hit: bool,
                       request_id: str, external: bool = True) -> None:
        if provider == "twelve_data":
            if cache_hit:
                self.counters["twelve_data_cache_hits"] += 1
            else:
                self.counters["twelve_data_cache_misses"] += 1
            if external:
                self.counters["twelve_data_requests_total"] += 1
                self.by_timeframe[timeframe] += 1
        self.requests.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": provider, "symbol": symbol, "timeframe": timeframe,
            "caller": caller, "reason": reason, "cache_hit": cache_hit,
            "request_id": request_id,
        })

    def snapshot(self) -> dict:
        required = {
            key: int(self.counters.get(key, 0)) for key in (
                "twelve_data_requests_total", "twelve_data_cache_hits",
                "twelve_data_cache_misses", "twelve_data_429_total",
                "twelve_data_retry_total", "analysis_failures_total",
                "telegram_error_notifications_total")}
        return {
            **required,
            "twelve_data_requests_by_timeframe": dict(self.by_timeframe),
            "twelve_data_circuit_state": self.circuit_state,
            "market_data_age_seconds": self.market_data_age_seconds,
            "recent_request_sources": list(self.requests)[-25:],
        }

    def reset(self) -> None:
        self.counters.clear()
        self.by_timeframe.clear()
        self.requests.clear()
        self.circuit_state = "CLOSED"
        self.market_data_age_seconds = None


metrics = MarketDataMetrics()

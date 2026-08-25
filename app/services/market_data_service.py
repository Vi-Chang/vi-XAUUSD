"""Canonical quote/candle service shared by APIs, schedulers and strategies."""
from __future__ import annotations

import asyncio
import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.providers.base import Candle, MarketDataProvider, PriceTick, ProviderError
from app.services.market_data_metrics import metrics
from app.utils.timeutils import TIMEFRAME_MINUTES

request_context: ContextVar[dict | None] = ContextVar(
    "market_data_request_context", default=None)


class RequestDeduplicator:
    """All concurrent callers for one provider/symbol/timeframe await one task."""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight: dict[str, asyncio.Task] = {}

    def _loop_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._lock, self._inflight = loop, asyncio.Lock(), {}
        assert self._lock is not None
        return self._lock

    async def run(self, key: str, factory):
        async with self._loop_lock():
            task = self._inflight.get(key)
            if task is None or task.done():
                task = asyncio.create_task(factory())
                self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._inflight.get(key) is task:
                self._inflight.pop(key, None)


@dataclass
class _CandleCache:
    fetched_at: datetime
    candles: list[Candle]


class MarketDataService(MarketDataProvider):
    """Provider adapter with canonical cache, incremental merge and LKG fallback."""

    realtime_capable = True

    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider
        self.name = provider.name
        self.realtime_capable = provider.realtime_capable
        self.min_poll_seconds = getattr(provider, "min_poll_seconds", 0)
        self._dedup = RequestDeduplicator()
        self._candles: dict[tuple[str, str], _CandleCache] = {}
        self._quote: dict[str, PriceTick] = {}
        self._health: dict[str, dict] = {}
        self._recovery_pending = False
        self._recovery_synced: set[str] = set()
        self._external_fetch_allowed = True

    def set_external_fetch_allowed(self, allowed: bool) -> None:
        """Followers may read shared/LKG data but must not spend provider quota."""
        self._external_fetch_allowed = bool(allowed)

    @staticmethod
    def _refresh_seconds(timeframe: str) -> int:
        return int(get_settings().market_data_refresh_seconds.get(timeframe, 120))

    @staticmethod
    def _same_candle_window(timeframe: str, first: datetime, second: datetime) -> bool:
        minutes = TIMEFRAME_MINUTES.get(timeframe)
        if not minutes:
            return (second - first) < timedelta(seconds=MarketDataService._refresh_seconds(timeframe))
        same_bucket = (
            int(first.timestamp()) // (minutes * 60)
            == int(second.timestamp()) // (minutes * 60))
        return same_bucket and (second - first) < timedelta(
            seconds=MarketDataService._refresh_seconds(timeframe))

    async def get_quote(self, symbol: str = "XAUUSD", *, caller: str = "unknown",
                        reason: str = "quote") -> PriceTick:
        now = datetime.now(timezone.utc)
        cached = self._quote.get(symbol)
        ttl = max(1, int(get_settings().tier1_quote_seconds),
                  int(getattr(self.provider, "min_poll_seconds", 0) or 0))
        if cached is None:
            cached = self._load_shared_quote(symbol, ttl, now)
            if cached:
                self._quote[symbol] = cached
        if cached and (now - cached.quote_time).total_seconds() < ttl:
            self._record("QUOTE", caller, reason, True, symbol)
            return cached

        async def fetch():
            if not self._external_fetch_allowed:
                raise ProviderError("此副本僅讀取共享行情，不執行外部報價請求")
            token = request_context.set({"caller": caller, "reason": reason,
                                         "timeframe": "QUOTE", "priority": "P0"})
            try:
                tick = await self.provider.get_live_price(symbol)
                self._consume_provider_recovery("QUOTE")
                self._quote[symbol] = tick
                quote_version = tick.quote_time.isoformat()
                quote_checksum = hashlib.sha256(
                    f"{tick.bid}:{tick.ask}:{quote_version}".encode()).hexdigest()[:16]
                self._health["QUOTE"] = self._healthy(
                    tick.quote_time, quote_checksum, quote_version)
                return tick
            except Exception as exc:
                if cached:
                    quote_version = cached.quote_time.isoformat()
                    quote_checksum = hashlib.sha256(
                        f"{cached.bid}:{cached.ask}:{quote_version}".encode()
                    ).hexdigest()[:16]
                    self._health["QUOTE"] = self._degraded(
                        cached.quote_time, exc, quote_checksum, quote_version)
                    return cached
                raise
            finally:
                request_context.reset(token)

        return await self._dedup.run(f"{self.name}:{symbol}:QUOTE", fetch)

    def _load_shared_quote(self, symbol: str, ttl: int,
                           now: datetime) -> PriceTick | None:
        """Reuse a recent quote written by the scheduler owner/another replica."""
        try:
            from sqlalchemy import select

            from app.db.models import LivePrice
            from app.db.session import db_session
            with db_session() as db:
                row = db.execute(select(LivePrice).where(
                    LivePrice.symbol == symbol,
                    LivePrice.provider == self.name,
                ).order_by(LivePrice.received_at.desc()).limit(1)).scalar_one_or_none()
            if row is None:
                return None
            received = row.received_at
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            if (now - received).total_seconds() >= ttl:
                return None
            quote_time = row.quote_time
            if quote_time.tzinfo is None:
                quote_time = quote_time.replace(tzinfo=timezone.utc)
            return PriceTick(symbol=symbol, bid=row.bid, ask=row.ask,
                             quote_time=quote_time, provider=row.provider)
        except Exception:  # noqa: BLE001 - DB cache is optional
            return None

    async def get_live_price(self, symbol: str = "XAUUSD") -> PriceTick:
        return await self.get_quote(symbol, caller="provider_adapter", reason="live_price")

    async def get_candles(self, symbol: str = "XAUUSD", timeframe: str = "15M",
                          count: int = 300, *, caller: str = "unknown",
                          reason: str = "analysis") -> list[Candle]:
        key = (symbol, timeframe)
        cached = self._candles.get(key)
        now = datetime.now(timezone.utc)
        if (cached and not self._recovery_pending
                and self._same_candle_window(timeframe, cached.fetched_at, now)):
            self._record(timeframe, caller, reason, True, symbol)
            return cached.candles[-count:]

        async def fetch():
            existing = self._candles.get(key)
            if not self._external_fetch_allowed:
                if existing and existing.candles:
                    return existing.candles[-count:]
                raise ProviderError("此副本僅讀取共享行情，不執行外部 K 棒請求")
            initial = existing is None or len(existing.candles) < count
            requested = count if (initial or self._recovery_pending) else min(
                get_settings().market_data_incremental_candle_count, count)
            priority = "P1" if timeframe in {"15M", "1H"} else (
                "P2" if timeframe in {"4H", "1D", "5M"} else "P3")
            token = request_context.set({"caller": caller, "reason": reason,
                                         "timeframe": timeframe, "priority": priority})
            try:
                incoming = await self.provider.get_candles(symbol, timeframe, requested)
                self._consume_provider_recovery(timeframe)
                merged = self._merge(existing.candles if existing else [], incoming, count)
                self._candles[key] = _CandleCache(now, merged)
                latest, checksum, version = self._candle_identity(merged, now)
                self._health[timeframe] = self._healthy(latest, checksum, version)
                if self._recovery_pending:
                    self._recovery_synced.add(timeframe)
                return merged[-count:]
            except Exception as exc:
                if existing and existing.candles:
                    latest, checksum, version = self._candle_identity(
                        existing.candles, now)
                    self._health[timeframe] = self._degraded(
                        latest, exc, checksum, version)
                    return existing.candles[-count:]
                raise
            finally:
                request_context.reset(token)

        return await self._dedup.run(f"{self.name}:{symbol}:{timeframe}", fetch)

    @staticmethod
    def _merge(existing: list[Candle], incoming: list[Candle], count: int) -> list[Candle]:
        by_open = {item.open_time: item for item in existing}
        by_open.update({item.open_time: item for item in incoming})
        return [by_open[key] for key in sorted(by_open)][-count:]

    @staticmethod
    def _candle_identity(candles: list[Candle], fallback: datetime) -> tuple:
        closed = [item for item in candles if item.is_closed]
        latest = (closed[-1] if closed else candles[-1]) if candles else None
        timestamp = latest.close_time if latest else fallback
        version = timestamp.isoformat()
        raw = ":".join([
            str(len(candles)),
            latest.open_time.isoformat() if latest else version,
            str(latest.close) if latest else "",
        ])
        return timestamp, hashlib.sha256(raw.encode()).hexdigest()[:16], version

    @staticmethod
    def _healthy(timestamp: datetime, checksum: str = "", version: str = "") -> dict:
        return {"status": "GOOD", "freshness": "FRESH",
                "lastSuccessAt": timestamp.isoformat(), "errorType": "",
                "checksum": checksum, "version": version or timestamp.isoformat()}

    @staticmethod
    def _degraded(timestamp: datetime, exc: Exception, checksum: str = "",
                  version: str = "") -> dict:
        return {"status": "DEGRADED", "freshness": "STALE",
                "lastSuccessAt": timestamp.isoformat(),
                "errorType": type(exc).__name__, "checksum": checksum,
                "version": version or timestamp.isoformat()}

    def health_snapshot(self) -> dict:
        core_names = {"QUOTE", "15M", "1H", "4H", "1D"}
        core = [item for key, item in self._health.items() if key in core_names]
        optional = [item for key, item in self._health.items() if key not in core_names]
        overall = "DEGRADED" if any(
            item.get("status") != "GOOD" for item in core) else "GOOD"
        latest_times = []
        for item in core:
            try:
                latest_times.append(datetime.fromisoformat(
                    str(item.get("lastSuccessAt") or "").replace("Z", "+00:00")))
            except ValueError:
                pass
        if latest_times:
            metrics.market_data_age_seconds = round(max(
                0.0, (datetime.now(timezone.utc) - max(latest_times)).total_seconds()), 1)
        optional_degraded = any(item.get("status") != "GOOD" for item in optional)
        return {"status": overall, "coreStatus": overall,
                "analysisHealth": (
                    "DEGRADED" if overall == "DEGRADED" or optional_degraded else "GOOD"),
                "provider": self.name,
                "timeframes": dict(self._health),
                "optionalDegraded": optional_degraded,
                "recoverySyncPending": self._recovery_pending}

    def last_known_good(self, timeframe: str, symbol: str = "XAUUSD") -> list[Candle]:
        cached = self._candles.get((symbol, timeframe))
        return list(cached.candles) if cached else []

    def prime_candles(self, timeframe: str, candles: list[Candle],
                      symbol: str = "XAUUSD") -> None:
        """Seed process cache from durable DB, then force a small sync."""
        if candles and (symbol, timeframe) not in self._candles:
            stale_fetch = datetime.now(timezone.utc) - timedelta(
                seconds=self._refresh_seconds(timeframe) + 1)
            self._candles[(symbol, timeframe)] = _CandleCache(stale_fetch, candles)
            prime = getattr(self.provider, "prime_candles", None)
            if callable(prime):
                prime(timeframe, candles)

    def mark_timeframe_degraded(self, timeframe: str, exc: Exception,
                                candles: list[Candle]) -> None:
        timestamp, checksum, version = self._candle_identity(
            candles, datetime.now(timezone.utc))
        self._health[timeframe] = self._degraded(
            timestamp, exc, checksum, version)

    def _consume_provider_recovery(self, timeframe: str) -> None:
        consume = getattr(self.provider, "consume_recovery_signal", None)
        if callable(consume) and consume():
            self._recovery_pending = True
            self._recovery_synced = {timeframe}

    def complete_recovery_sync(self, timeframes: tuple[str, ...]) -> bool:
        if not self._recovery_pending:
            return False
        core = {"15M", "1H", "4H", "1D"} & set(timeframes)
        if core.issubset(self._recovery_synced):
            self._recovery_pending = False
            return True
        return False

    def _record(self, timeframe: str, caller: str, reason: str,
                hit: bool, symbol: str) -> None:
        seed = f"{self.name}:{symbol}:{timeframe}:{caller}:{datetime.now(timezone.utc).isoformat()}"
        metrics.record_request(
            provider=self.name, symbol=symbol, timeframe=timeframe,
            caller=caller, reason=reason, cache_hit=hit,
            request_id=hashlib.sha256(seed.encode()).hexdigest()[:16],
            external=False)

    async def close(self) -> None:
        await self.provider.close()


def as_market_data_service(provider: MarketDataProvider) -> MarketDataService:
    return provider if isinstance(provider, MarketDataService) else MarketDataService(provider)

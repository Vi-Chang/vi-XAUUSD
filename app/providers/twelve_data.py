"""Twelve Data Adapter(spec 二之2)— 免費層 800 次/日、8 次/分。

可作為備援,也可作為「無券商帳戶」模式的主力(PRIMARY_PROVIDER=twelve_data)。
主力模式的配額預算(交易時段 ~23h/日):
- 即時價每 5 分鐘 1 次(min_poll_seconds=300)≈ 276 次
- K 棒收線才重抓(K 棒邊界快取)::15M≈96、30M≈48、1H≈24、4H≈6
- 1D/1W 由長 1H(outputsize 5000)本地聚合,每 6 小時刷新 ≈ 4 次
合計 ≈ 450 次/日 < 800。已知限制:免費層無 bid/ask(以 mid 近似,spread 檢查停用)。

配額計數為**全域共享**(QuotaTracker 單例),避免多個實例各算各的而爆量。
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.config import get_settings
from app.providers.base import (
    Candle,
    MarketDataProvider,
    PriceTick,
    ProviderError,
    ProviderRateLimitedError,
    QuotaExceededError,
)
from app.utils.timeutils import TIMEFRAME_MINUTES

logger = logging.getLogger(__name__)

INTERVAL = {"5M": "5min", "15M": "15min", "30M": "30min", "1H": "1h", "4H": "4h"}
LONG_H1_KEY = "1H_LONG"          # 供 1D/1W 聚合的長 1H 快取
LONG_H1_REFRESH = timedelta(hours=6)
LONG_H1_SIZE = 5000              # ≈217 天的 1H,足夠 200+ 根日線


def bar_floor(t: datetime, minutes: int) -> datetime:
    """對齊到 K 棒邊界(UTC)。"""
    return t - timedelta(minutes=(t.hour * 60 + t.minute) % minutes,
                         seconds=t.second, microseconds=t.microsecond)


def needs_refetch(timeframe: str, last_fetch: datetime | None, now: datetime) -> bool:
    """自上次抓取後是否已跨過新 K 棒邊界(否則直接用快取,省配額)。"""
    if last_fetch is None:
        return True
    if timeframe == LONG_H1_KEY:
        return now - last_fetch >= LONG_H1_REFRESH
    return bar_floor(now, TIMEFRAME_MINUTES[timeframe]) > bar_floor(
        last_fetch, TIMEFRAME_MINUTES[timeframe])


class QuotaTracker:
    """免費層配額計數(日 / 分鐘雙重上限)。"""

    def __init__(self, daily_limit: int, minute_limit: int) -> None:
        self.daily_limit = daily_limit
        self.minute_limit = minute_limit
        self._day: date | None = None
        self._day_count = 0
        self._minute_stamps: list[datetime] = []

    def check_and_count(self, *, priority: str = "P1") -> None:
        now = datetime.now(timezone.utc)
        if self._day != now.date():
            self._day, self._day_count = now.date(), 0
        self._minute_stamps = [t for t in self._minute_stamps if now - t < timedelta(minutes=1)]
        if self._day_count >= self.daily_limit:
            raise QuotaExceededError("Twelve Data 每日配額已用盡")
        if len(self._minute_stamps) >= self.minute_limit:
            raise QuotaExceededError("Twelve Data 每分鐘配額已用盡")
        remaining_minute = self.minute_limit - len(self._minute_stamps)
        # Preserve the last slot for quotes/core confirmation. Optional and
        # background timeframes degrade before they can starve P0/P1 traffic.
        if priority == "P3" and remaining_minute <= 2:
            raise QuotaExceededError("Twelve Data 分鐘預算保留給核心週期")
        if priority == "P2" and remaining_minute <= 1:
            raise QuotaExceededError("Twelve Data 分鐘預算保留給即時報價")
        self._day_count += 1
        self._minute_stamps.append(now)

    @property
    def used_today(self) -> int:
        return self._day_count

    @property
    def remaining_today(self) -> int:
        return max(0, self.daily_limit - self._day_count)

    @property
    def requests_last_minute(self) -> int:
        now = datetime.now(timezone.utc)
        return len([item for item in self._minute_stamps
                    if now - item < timedelta(minutes=1)])


class TwelveDataCircuitBreaker:
    """Global CLOSED/OPEN/HALF_OPEN provider gate with one probe."""

    def __init__(self) -> None:
        self.state = "CLOSED"
        self.open_until: datetime | None = None
        self.rate_limit_count = 0
        self.probe_inflight = False

    def before_request(self) -> None:
        now = datetime.now(timezone.utc)
        if self.state == "OPEN":
            if self.open_until and now < self.open_until:
                retry = max(0.0, (self.open_until - now).total_seconds())
                raise ProviderRateLimitedError(
                    "Twelve Data 暫停外部請求，改用快取", retry_after=retry)
            self.state = "HALF_OPEN"
        if self.state == "HALF_OPEN":
            if self.probe_inflight:
                raise ProviderRateLimitedError("Twelve Data 恢復探測進行中")
            self.probe_inflight = True
        from app.services.market_data_metrics import metrics
        metrics.circuit_state = self.state

    def rate_limited(self, retry_after: float | None = None) -> float:
        settings = get_settings()
        self.rate_limit_count += 1
        fallback = min(
            settings.twelve_data_rate_limit_max_backoff_seconds,
            settings.twelve_data_rate_limit_base_backoff_seconds
            * (2 ** max(0, self.rate_limit_count - 1)),
        )
        delay = max(float(retry_after or 0), float(fallback))
        delay *= 1 + random.random() * settings.twelve_data_rate_limit_jitter_ratio
        self.state, self.probe_inflight = "OPEN", False
        self.open_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
        from app.services.market_data_metrics import metrics
        metrics.circuit_state = self.state
        return delay

    def success(self) -> bool:
        recovered = self.state in {"OPEN", "HALF_OPEN"}
        self.state, self.open_until = "CLOSED", None
        self.rate_limit_count, self.probe_inflight = 0, False
        from app.services.market_data_metrics import metrics
        metrics.circuit_state = self.state
        return recovered

    def transient_failure(self) -> None:
        if self.state == "HALF_OPEN":
            self.rate_limited()


_shared_circuit = TwelveDataCircuitBreaker()


def get_shared_circuit_breaker() -> TwelveDataCircuitBreaker:
    return _shared_circuit


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


_shared_quota: QuotaTracker | None = None


def get_shared_quota() -> QuotaTracker:
    """全域共享配額(所有 TwelveDataProvider 實例共用同一計數)。"""
    global _shared_quota
    if _shared_quota is None:
        s = get_settings()
        _shared_quota = QuotaTracker(s.twelve_data_daily_limit, s.twelve_data_minute_limit)
    return _shared_quota


class TwelveDataProvider(MarketDataProvider):
    name = "twelve_data"
    realtime_capable = True
    #: 免費層 8 次/分 → 主力模式下即時價最快 5 分鐘一輪(排程器會讀取此值)
    min_poll_seconds = 300

    def __init__(self) -> None:
        s = get_settings()
        if not s.twelve_data_api_key:
            raise ProviderError("TWELVE_DATA_API_KEY 未設定(https://twelvedata.com 免費註冊)")
        self._key = s.twelve_data_api_key
        self.quota = get_shared_quota()
        self.circuit = get_shared_circuit_breaker()
        self._recovery_signal = False
        self._client = httpx.AsyncClient(base_url="https://api.twelvedata.com", timeout=15.0)
        # K 棒邊界快取:key = timeframe 或 LONG_H1_KEY → (fetch_time, candles)
        self._cache: dict[str, tuple[datetime, list[Candle]]] = {}

    async def get_live_price(self, symbol: str = "XAUUSD") -> PriceTick:
        data = await self._request(
            "/price", {"symbol": "XAU/USD"}, timeframe="QUOTE", priority="P0")
        if "price" not in data:
            raise ProviderError("Twelve Data 報價回應缺少 price")
        mid = float(data["price"])
        return PriceTick(symbol=symbol, bid=mid, ask=mid,
                         quote_time=datetime.now(timezone.utc), provider=self.name)

    async def _request(self, path: str, params: dict, *, timeframe: str,
                       priority: str) -> dict:
        from app.services.market_data_metrics import metrics
        from app.services.market_data_service import request_context
        from app.services.secret_sanitizer import sanitize_text
        context = request_context.get() or {}
        caller = str(context.get("caller") or "direct_provider_call")
        reason = str(context.get("reason") or "market_data")
        priority = str(context.get("priority") or priority)
        attempts = max(1, get_settings().twelve_data_transient_retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            self.circuit.before_request()
            self.quota.check_and_count(priority=priority)
            request_id = uuid.uuid4().hex[:16]
            metrics.record_request(
                provider=self.name, symbol="XAUUSD", timeframe=timeframe,
                caller=caller, reason=reason, cache_hit=False,
                request_id=request_id, external=True)
            from app.services.api_counter import bump
            bump(self.name)
            try:
                response = await self._client.get(
                    path, params={**params, "apikey": self._key, "timezone": "UTC"})
                if response.status_code == 429:
                    metrics.counters["twelve_data_429_total"] += 1
                    delay = self.circuit.rate_limited(
                        _retry_after_seconds(response.headers.get("Retry-After")))
                    raise ProviderRateLimitedError(
                        "Twelve Data 流量限制，暫時改用最後有效資料",
                        retry_after=delay)
                response.raise_for_status()
                payload = response.json()
                if payload.get("status") == "error":
                    raise ProviderError(
                        sanitize_text(f"Twelve Data: {payload.get('message') or 'unknown error'}"))
                if self.circuit.success():
                    self._recovery_signal = True
                return payload
            except ProviderRateLimitedError:
                raise
            except Exception as exc:  # noqa: BLE001 - network adapter boundary
                last_error = exc
                self.circuit.transient_failure()
                if attempt + 1 >= attempts:
                    break
                metrics.counters["twelve_data_retry_total"] += 1
                await asyncio.sleep((1 + random.random() * 0.3) * (2 ** attempt))
        # Do not retain the original httpx exception as __cause__: it may
        # contain the fully rendered request URL (including the API key).
        raise ProviderError(
            f"Twelve Data 暫時無法取得資料：{sanitize_text(last_error)}") from None

    async def _fetch_series(self, interval: str, outputsize: int,
                            timeframe_label: str) -> list[Candle]:
        priority = "P1" if timeframe_label in {"15M", "1H"} else (
            "P2" if timeframe_label in {"4H", "1D", "5M"} else "P3")
        data = await self._request(
            "/time_series", {"symbol": "XAU/USD", "interval": interval,
                             "outputsize": outputsize},
            timeframe=timeframe_label, priority=priority)
        from app.services.candle_service import candle_close_time
        now = datetime.now(timezone.utc)
        out: list[Candle] = []
        for row in reversed(data.get("values", [])):
            open_time = datetime.fromisoformat(row["datetime"]).replace(tzinfo=timezone.utc)
            close_time = candle_close_time(open_time, timeframe_label)
            out.append(Candle(
                symbol="XAUUSD", timeframe=timeframe_label,
                open_time=open_time, close_time=close_time,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume") or 0),
                is_closed=close_time <= now, data_provider=self.name))
        from app.services.candle_service import filter_market_hours
        return filter_market_hours(out)

    async def _long_h1(self) -> list[Candle]:
        now = datetime.now(timezone.utc)
        cached = self._cache.get(LONG_H1_KEY)
        if cached and not needs_refetch(LONG_H1_KEY, cached[0], now):
            return cached[1]
        candles = await self._fetch_series("1h", LONG_H1_SIZE, "1H")
        self._cache[LONG_H1_KEY] = (now, candles)
        return candles

    async def get_candles(self, symbol: str = "XAUUSD", timeframe: str = "15M",
                          count: int = 300) -> list[Candle]:
        now = datetime.now(timezone.utc)

        # One canonical 1H history powers 1H analysis plus local 1D/1W
        # aggregation.  This removes the former duplicate ordinary-1H and
        # LONG_H1 downloads during cold start.
        if timeframe == "1H":
            long_cached = self._cache.get(LONG_H1_KEY)
            if long_cached is None:
                return (await self._long_h1())[-count:]
            cached = self._cache.get("1H")
            if cached and not needs_refetch("1H", cached[0], now):
                return cached[1][-count:]
            incoming = await self._fetch_series(
                INTERVAL["1H"], min(count, 5000), "1H")
            merged = {item.open_time: item for item in long_cached[1]}
            merged.update({item.open_time: item for item in incoming})
            history = [merged[key] for key in sorted(merged)][-LONG_H1_SIZE:]
            self._cache[LONG_H1_KEY] = (now, history)
            self._cache["1H"] = (now, history[-max(count, 300):])
            return history[-count:]

        # 1D/1W:由長 1H 本地聚合(NY 17:00 ET 切分,spec 三)
        if timeframe in ("1D", "1W"):
            h1 = await self._long_h1()
            from app.services.candle_service import aggregate_candles
            closed = [c for c in h1 if c.is_closed]
            if h1 and not h1[-1].is_closed:
                closed.append(h1[-1])
            return aggregate_candles(closed, timeframe)[-count:]

        cached = self._cache.get(timeframe)
        if cached and not needs_refetch(timeframe, cached[0], now):
            return cached[1][-count:]
        candles = await self._fetch_series(INTERVAL[timeframe], min(count, 5000), timeframe)
        self._cache[timeframe] = (now, candles)
        return candles[-count:]

    async def close(self) -> None:
        await self._client.aclose()

    def consume_recovery_signal(self) -> bool:
        recovered, self._recovery_signal = self._recovery_signal, False
        return recovered

    def prime_candles(self, timeframe: str, candles: list[Candle]) -> None:
        """Seed adapter cache from durable canonical candles after restart."""
        if not candles:
            return
        stale = datetime.now(timezone.utc) - timedelta(
            minutes=TIMEFRAME_MINUTES.get(timeframe, 60) + 1)
        if timeframe == "1H":
            self._cache[LONG_H1_KEY] = (stale, candles[-LONG_H1_SIZE:])
        if timeframe not in {"1D", "1W"}:
            self._cache[timeframe] = (stale, candles)

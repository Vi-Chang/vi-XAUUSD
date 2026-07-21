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

import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from app.config import get_settings
from app.providers.base import (
    Candle, MarketDataProvider, PriceTick, ProviderError, QuotaExceededError, with_retry,
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

    def check_and_count(self) -> None:
        now = datetime.now(timezone.utc)
        if self._day != now.date():
            self._day, self._day_count = now.date(), 0
        self._minute_stamps = [t for t in self._minute_stamps if now - t < timedelta(minutes=1)]
        if self._day_count >= self.daily_limit:
            raise QuotaExceededError("Twelve Data 每日配額已用盡")
        if len(self._minute_stamps) >= self.minute_limit:
            raise QuotaExceededError("Twelve Data 每分鐘配額已用盡")
        self._day_count += 1
        self._minute_stamps.append(now)

    @property
    def used_today(self) -> int:
        return self._day_count

    @property
    def remaining_today(self) -> int:
        return max(0, self.daily_limit - self._day_count)


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
        self._client = httpx.AsyncClient(base_url="https://api.twelvedata.com", timeout=15.0)
        # K 棒邊界快取:key = timeframe 或 LONG_H1_KEY → (fetch_time, candles)
        self._cache: dict[str, tuple[datetime, list[Candle]]] = {}

    async def get_live_price(self, symbol: str = "XAUUSD") -> PriceTick:
        self.quota.check_and_count()
        from app.services.api_counter import bump
        bump(self.name)

        async def _call() -> PriceTick:
            r = await self._client.get("/price", params={"symbol": "XAU/USD", "apikey": self._key})
            r.raise_for_status()
            data = r.json()
            if "price" not in data:
                raise ProviderError(f"Twelve Data 回應異常: {data}")
            mid = float(data["price"])
            # 免費層無 bid/ask;以 mid 近似並由 provider 名稱標示(spread 檢查不適用)
            return PriceTick(symbol=symbol, bid=mid, ask=mid,
                            quote_time=datetime.now(timezone.utc), provider=self.name)

        return await with_retry(_call, retries=1, provider=self.name)

    async def _fetch_series(self, interval: str, outputsize: int,
                            timeframe_label: str) -> list[Candle]:
        self.quota.check_and_count()
        from app.services.api_counter import bump
        bump(self.name)

        async def _call() -> list[Candle]:
            r = await self._client.get("/time_series", params={
                "symbol": "XAU/USD", "interval": interval,
                "outputsize": outputsize, "apikey": self._key, "timezone": "UTC",
            })
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "error":
                raise ProviderError(f"Twelve Data: {data.get('message')}")
            from app.services.candle_service import candle_close_time
            now = datetime.now(timezone.utc)
            out: list[Candle] = []
            for row in reversed(data.get("values", [])):  # API 回傳新→舊,轉為升冪
                open_time = datetime.fromisoformat(row["datetime"]).replace(tzinfo=timezone.utc)
                close_time = candle_close_time(open_time, timeframe_label)
                out.append(Candle(
                    symbol="XAUUSD", timeframe=timeframe_label,
                    open_time=open_time, close_time=close_time,
                    open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                    is_closed=close_time <= now,
                    data_provider=self.name,
                ))
            # Twelve Data 於週末/休市仍發布殭屍報價 → 一律濾掉(spec 四)
            from app.services.candle_service import filter_market_hours
            return filter_market_hours(out)

        return await with_retry(_call, retries=1, provider=self.name)

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

"""模擬資料 Provider(spec 二十七之15)— 無任何 API Key 也能展示系統。

以固定 seed 的隨機漫步產生 XAUUSD K 棒與即時報價;
各週期由同一條 5 分鐘路徑聚合而成,確保跨週期一致、
日線依 NY 17:00 ET 切分,行為與真實資料相同。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.providers.base import Candle, MarketDataProvider, PriceTick
from app.utils.timeutils import TIMEFRAME_MINUTES, is_market_open, trading_day

BASE_PRICE = 4680.0
SEED = 20260718


class MockProvider(MarketDataProvider):
    name = "mock"
    realtime_capable = True  # 模擬「即時」;僅限展示模式

    def __init__(self) -> None:
        self._m5: list[Candle] | None = None

    def _build_m5_path(self, bars: int = 12000) -> list[Candle]:
        """產生近似真實波動的 5 分鐘路徑(僅交易時段)。"""
        rng = random.Random(SEED)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        now -= timedelta(minutes=now.minute % 5)
        # 從現在往回鋪 bars 根交易時段內的 5M K 棒
        times: list[datetime] = []
        t = now
        while len(times) < bars:
            if is_market_open(t):
                times.append(t)
            t -= timedelta(minutes=5)
        times.reverse()

        out: list[Candle] = []
        price = BASE_PRICE
        trend = 0.0
        for i, open_time in enumerate(times):
            # 混合趨勢 + 均值回歸,製造可辨識的 swing 結構
            if i % 240 == 0:
                trend = rng.uniform(-0.35, 0.35)
            drift = trend + rng.gauss(0, 1.8)
            o = price
            c = max(1.0, o + drift)
            hi = max(o, c) + abs(rng.gauss(0, 0.9))
            lo = min(o, c) - abs(rng.gauss(0, 0.9))
            spread = round(rng.uniform(0.25, 0.45), 2)
            out.append(Candle(
                symbol="XAUUSD", timeframe="5M",
                open_time=open_time, close_time=open_time + timedelta(minutes=5),
                open=round(o, 2), high=round(hi, 2), low=round(lo, 2), close=round(c, 2),
                volume=float(rng.randint(200, 2500)),  # Tick Volume
                bid_close=round(c - spread / 2, 2), ask_close=round(c + spread / 2, 2),
                spread=spread, is_closed=True, data_provider=self.name,
            ))
            price = c
        # 最後一根視為未收線
        if out:
            out[-1].is_closed = False
        return out

    def _m5_path(self) -> list[Candle]:
        if self._m5 is None:
            self._m5 = self._build_m5_path()
        return self._m5

    @staticmethod
    def _bucket_key(c: Candle, timeframe: str):
        if timeframe == "1D":
            return trading_day(c.open_time)
        if timeframe == "1W":
            d = trading_day(c.open_time)
            return d - timedelta(days=d.weekday())
        minutes = TIMEFRAME_MINUTES[timeframe]
        t = c.open_time
        floored = t - timedelta(minutes=(t.hour * 60 + t.minute) % minutes,
                                seconds=t.second, microseconds=t.microsecond)
        return floored

    async def get_candles(self, symbol: str = "XAUUSD", timeframe: str = "15M",
                          count: int = 300) -> list[Candle]:
        m5 = self._m5_path()
        if timeframe == "5M":
            return m5[-count:]
        buckets: dict = {}
        order: list = []
        for c in m5:
            key = self._bucket_key(c, timeframe)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(c)

        out: list[Candle] = []
        for key in order:
            group = buckets[key]
            first, last = group[0], group[-1]
            out.append(Candle(
                symbol=symbol, timeframe=timeframe,
                open_time=first.open_time, close_time=last.close_time,
                open=first.open, high=max(c.high for c in group),
                low=min(c.low for c in group), close=last.close,
                volume=sum(c.volume for c in group),
                bid_close=last.bid_close, ask_close=last.ask_close, spread=last.spread,
                is_closed=all(c.is_closed for c in group),
                data_provider=self.name,
            ))
        return out[-count:]

    async def get_live_price(self, symbol: str = "XAUUSD") -> PriceTick:
        last = self._m5_path()[-1]
        return PriceTick(symbol=symbol,
                        bid=last.bid_close or last.close - 0.15,
                        ask=last.ask_close or last.close + 0.15,
                        quote_time=datetime.now(timezone.utc), provider=self.name)

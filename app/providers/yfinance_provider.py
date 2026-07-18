"""yfinance Adapter(spec 二之3)— 僅供離線開發 / Demo / 歷史回測。

明確標記:資料可能延遲且 GC=F 為期貨價格,與現貨存在基差,
禁止用於即時分析(realtime_capable=False,呼叫端必須檢查)。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.providers.base import Candle, MarketDataProvider, PriceTick, ProviderError

logger = logging.getLogger(__name__)

YF_INTERVAL = {"5M": "5m", "15M": "15m", "30M": "30m", "1H": "1h", "4H": "4h",
               "1D": "1d", "1W": "1wk"}
YF_PERIOD = {"5M": "5d", "15M": "1mo", "30M": "1mo", "1H": "3mo", "4H": "6mo",
             "1D": "2y", "1W": "5y"}


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"
    realtime_capable = False  # 延遲 + 期貨基差:僅開發/回測
    FUTURES_BASIS_WARNING = "yfinance GC=F 為期貨價格,與現貨有基差;僅供開發/回測"

    def __init__(self, ticker: str = "GC=F") -> None:
        self._ticker = ticker

    async def get_live_price(self, symbol: str = "XAUUSD") -> PriceTick:
        candles = await self.get_candles(symbol, "15M", count=2)
        if not candles:
            raise ProviderError("yfinance 無資料")
        last = candles[-1]
        return PriceTick(symbol=symbol, bid=last.close, ask=last.close,
                        quote_time=last.close_time, provider=self.name)

    async def get_candles(self, symbol: str = "XAUUSD", timeframe: str = "15M",
                          count: int = 300) -> list[Candle]:
        def _fetch() -> list[Candle]:
            import yfinance as yf
            from app.services.candle_service import candle_close_time
            df = yf.Ticker(self._ticker).history(
                period=YF_PERIOD[timeframe], interval=YF_INTERVAL[timeframe])
            if df.empty:
                raise ProviderError(f"yfinance {self._ticker} 無 {timeframe} 資料")
            df = df.tail(count)
            out: list[Candle] = []
            now = datetime.now(timezone.utc)
            for ts, row in df.iterrows():
                open_time = ts.to_pydatetime()
                if open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=timezone.utc)
                open_time = open_time.astimezone(timezone.utc)
                close_time = candle_close_time(open_time, timeframe)
                out.append(Candle(
                    symbol=symbol, timeframe=timeframe,
                    open_time=open_time, close_time=close_time,
                    open=float(row["Open"]), high=float(row["High"]),
                    low=float(row["Low"]), close=float(row["Close"]),
                    volume=float(row.get("Volume", 0)),
                    is_closed=close_time <= now,
                    data_provider=self.name,
                    extra={"warning": self.FUTURES_BASIS_WARNING},
                ))
            return out

        return await asyncio.to_thread(_fetch)

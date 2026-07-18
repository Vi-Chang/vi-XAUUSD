"""OANDA v20 Practice Adapter(spec 二之1)— 主力即時行情。

- REST:即時 Bid/Ask 報價、歷史 K 棒(price=BAM 同時取 Bid/Ask/Mid 蠟燭)。
- 日線/週線請求帶 dailyAlignment=17 + alignmentTimezone=America/New_York,
  與 spec 三之日線切分規則一致。
- 指數退避重試由 base.with_retry 提供。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.providers.base import Candle, MarketDataProvider, PriceTick, ProviderError, with_retry

logger = logging.getLogger(__name__)

GRANULARITY = {"5M": "M5", "15M": "M15", "30M": "M30", "1H": "H1", "4H": "H4", "1D": "D", "1W": "W"}


def _parse_time(s: str) -> datetime:
    # OANDA 回傳 RFC3339 帶納秒,截到微秒再解析
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".")
        s = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class OandaProvider(MarketDataProvider):
    name = "oanda"
    realtime_capable = True

    def __init__(self) -> None:
        s = get_settings()
        host = ("https://api-fxpractice.oanda.com" if s.oanda_env == "practice"
                else "https://api-fxtrade.oanda.com")
        if not s.oanda_api_token:
            raise ProviderError("OANDA_API_TOKEN 未設定(請註冊 Practice 帳戶取得免費 Token)")
        self._account_id = s.oanda_account_id
        self._client = httpx.AsyncClient(
            base_url=host,
            headers={"Authorization": f"Bearer {s.oanda_api_token}"},
            timeout=15.0,
        )

    async def get_live_price(self, symbol: str = "XAUUSD") -> PriceTick:
        instrument = symbol.replace("USD", "_USD") if "_" not in symbol else symbol

        async def _call() -> PriceTick:
            r = await self._client.get(
                f"/v3/accounts/{self._account_id}/pricing",
                params={"instruments": instrument},
            )
            r.raise_for_status()
            prices = r.json()["prices"]
            if not prices:
                raise ProviderError(f"OANDA 無 {instrument} 報價")
            p = prices[0]
            return PriceTick(
                symbol=symbol,
                bid=float(p["bids"][0]["price"]),
                ask=float(p["asks"][0]["price"]),
                quote_time=_parse_time(p["time"]),
                provider=self.name,
            )

        return await with_retry(_call, provider=self.name)

    async def get_candles(self, symbol: str = "XAUUSD", timeframe: str = "15M",
                          count: int = 300) -> list[Candle]:
        instrument = symbol.replace("USD", "_USD") if "_" not in symbol else symbol
        params: dict = {
            "granularity": GRANULARITY[timeframe],
            "count": min(count, 5000),
            "price": "BAM",  # Bid/Ask/Mid 蠟燭(spec:price=BA/BAM)
        }
        if timeframe in ("1D", "1W"):
            params["dailyAlignment"] = 17
            params["alignmentTimezone"] = "America/New_York"

        async def _call() -> list[Candle]:
            r = await self._client.get(f"/v3/instruments/{instrument}/candles", params=params)
            r.raise_for_status()
            out: list[Candle] = []
            from app.services.candle_service import candle_close_time
            for c in r.json()["candles"]:
                mid = c.get("mid") or {}
                bid = c.get("bid") or {}
                ask = c.get("ask") or {}
                open_time = _parse_time(c["time"])
                bid_close = float(bid["c"]) if bid else None
                ask_close = float(ask["c"]) if ask else None
                out.append(Candle(
                    symbol=symbol, timeframe=timeframe,
                    open_time=open_time,
                    close_time=candle_close_time(open_time, timeframe),
                    open=float(mid["o"]), high=float(mid["h"]),
                    low=float(mid["l"]), close=float(mid["c"]),
                    volume=float(c.get("volume", 0)),  # Tick Volume
                    bid_close=bid_close, ask_close=ask_close,
                    spread=(round(ask_close - bid_close, 3)
                            if bid_close is not None and ask_close is not None else None),
                    is_closed=bool(c.get("complete", False)),
                    data_provider=self.name,
                ))
            return out

        return await with_retry(_call, provider=self.name)

    async def get_transactions(self, since_id: str | None = None) -> list[dict]:
        """Practice 帳戶成交紀錄(Trading Coach 自動同步用;Phase 7 排程呼叫)。"""
        async def _call() -> list[dict]:
            params = {"sinceID": since_id} if since_id else {}
            r = await self._client.get(
                f"/v3/accounts/{self._account_id}/transactions", params=params)
            r.raise_for_status()
            return r.json().get("transactions", [])

        return await with_retry(_call, provider=self.name)

    async def close(self) -> None:
        await self._client.aclose()

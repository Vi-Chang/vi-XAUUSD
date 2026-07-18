"""Capital.com Provider(Demo 帳戶免費 REST API)— OANDA 的替代備選。

- 認證:X-CAP-API-KEY + POST /session(identifier + API Key 專用密碼)
  → 回應 header 的 CST / X-SECURITY-TOKEN 作為後續請求憑證。
  Session 閒置約 10 分鐘失效 → 401 時自動重新登入一次。
- 行情:GET /markets/{epic}(即時 bid/offer)、GET /prices/{epic}(歷史 K 棒,
  snapshotTimeUTC 為 UTC)。黃金 epic 預設 "GOLD"(= 現貨 XAUUSD)。
- 日/週線一律由 HOUR 資料依 NY 17:00 ET 本地聚合(與 MT5 Provider 同策略),
  不使用 Capital.com 的 DAY/WEEK 週期,避免切分不一致。
- 速率:官方一般端點約 10 req/s、session 1 req/s;本系統用量遠低於此。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import get_settings
from app.providers.base import Candle, MarketDataProvider, PriceTick, ProviderError, with_retry

logger = logging.getLogger(__name__)

RESOLUTION = {"5M": "MINUTE_5", "15M": "MINUTE_15", "30M": "MINUTE_30",
              "1H": "HOUR", "4H": "HOUR_4"}
MAX_PER_REQUEST = 1000


def parse_snapshot_time(s: str) -> datetime:
    """snapshotTimeUTC 形如 2026-07-20T08:15:00 → UTC datetime。"""
    return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=timezone.utc)


def candle_from_price_row(row: dict, symbol: str, timeframe: str,
                          now: datetime | None = None) -> Candle:
    """Capital.com /prices 單筆 → Candle(mid = (bid+ask)/2;volume 為 tick volume)。"""
    from app.services.candle_service import candle_close_time

    def mid(field: str) -> float:
        p = row[field]
        bid, ask = p.get("bid"), p.get("ask")
        if bid is None:
            return float(ask)
        if ask is None:
            return float(bid)
        return (float(bid) + float(ask)) / 2

    open_time = parse_snapshot_time(row.get("snapshotTimeUTC") or row["snapshotTime"])
    close_time = candle_close_time(open_time, timeframe)
    bid_c = row["closePrice"].get("bid")
    ask_c = row["closePrice"].get("ask")
    now = now or datetime.now(timezone.utc)
    return Candle(
        symbol=symbol, timeframe=timeframe,
        open_time=open_time, close_time=close_time,
        open=mid("openPrice"), high=mid("highPrice"),
        low=mid("lowPrice"), close=mid("closePrice"),
        volume=float(row.get("lastTradedVolume") or 0),  # Tick Volume
        bid_close=float(bid_c) if bid_c is not None else None,
        ask_close=float(ask_c) if ask_c is not None else None,
        spread=(round(float(ask_c) - float(bid_c), 3)
                if bid_c is not None and ask_c is not None else None),
        is_closed=close_time <= now,
        data_provider="capital_com",
    )


class CapitalComProvider(MarketDataProvider):
    name = "capital_com"
    realtime_capable = True

    def __init__(self) -> None:
        s = get_settings()
        if not (s.capital_api_key and s.capital_identifier and s.capital_api_password):
            raise ProviderError(
                "Capital.com 未設定:需要 CAPITAL_API_KEY / CAPITAL_IDENTIFIER / "
                "CAPITAL_API_PASSWORD(於平台 Settings → API integrations 產生)")
        base = ("https://demo-api-capital.backend-capital.com" if s.capital_demo
                else "https://api-capital.backend-capital.com")
        self._epic = s.capital_epic
        self._client = httpx.AsyncClient(
            base_url=f"{base}/api/v1", timeout=15.0,
            headers={"X-CAP-API-KEY": s.capital_api_key})
        self._session_lock = asyncio.Lock()

    async def _login(self) -> None:
        s = get_settings()
        r = await self._client.post("/session", json={
            "identifier": s.capital_identifier, "password": s.capital_api_password})
        if r.status_code != 200:
            raise ProviderError(f"Capital.com 登入失敗 {r.status_code}: {r.text[:200]}")
        self._client.headers["CST"] = r.headers.get("CST", "")
        self._client.headers["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
        logger.info("Capital.com session established (%s)",
                    "demo" if s.capital_demo else "live")

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """帶自動重新登入的 GET(session 逾時 → 401 → 重登一次)。"""
        if "CST" not in self._client.headers:
            async with self._session_lock:
                if "CST" not in self._client.headers:
                    await self._login()
        r = await self._client.get(path, params=params)
        if r.status_code == 401:
            async with self._session_lock:
                await self._login()
            r = await self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def get_live_price(self, symbol: str = "XAUUSD") -> PriceTick:
        async def _call() -> PriceTick:
            data = await self._get(f"/markets/{self._epic}")
            snap = data["snapshot"]
            ts = snap.get("updateTimeUTC") or snap.get("updateTime")
            try:
                quote_time = parse_snapshot_time(ts)
            except (TypeError, ValueError):
                quote_time = datetime.now(timezone.utc)
            return PriceTick(symbol=symbol, bid=float(snap["bid"]),
                            ask=float(snap["offer"]), quote_time=quote_time,
                            provider=self.name)

        return await with_retry(_call, provider=self.name)

    async def get_candles(self, symbol: str = "XAUUSD", timeframe: str = "15M",
                          count: int = 300) -> list[Candle]:
        # 日/週線:由 1H 本地聚合(NY 17:00 ET 切分)
        if timeframe in ("1D", "1W"):
            hours_needed = count * 24 if timeframe == "1D" else count * 24 * 7
            h1 = await self.get_candles(symbol, "1H", min(hours_needed, MAX_PER_REQUEST))
            from app.services.candle_service import aggregate_candles
            closed = [c for c in h1 if c.is_closed]
            if h1 and not h1[-1].is_closed:
                closed.append(h1[-1])
            return aggregate_candles(closed, timeframe)[-count:]

        async def _call() -> list[Candle]:
            data = await self._get(f"/prices/{self._epic}", params={
                "resolution": RESOLUTION[timeframe],
                "max": min(count, MAX_PER_REQUEST)})
            now = datetime.now(timezone.utc)
            out = [candle_from_price_row(row, symbol, timeframe, now)
                   for row in data.get("prices", [])]
            out.sort(key=lambda c: c.open_time)
            return out

        return await with_retry(_call, provider=self.name)

    async def close(self) -> None:
        await self._client.aclose()

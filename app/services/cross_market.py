"""跨市場迷你收集器(Phase 6 切片,V2 AI 巨集分析師輸入用)。

- 來源:yfinance(免費、無 Key):DXY=DX-Y.NYB、美十年債殖利率=^TNX、VIX=^VIX。
- 行程內快取 cross_market_cache_minutes 分鐘,失敗時回傳前次快取或空值,
  絕不讓跨市場資料失敗拖垮主分析(優雅降級)。
- 純程式計算;AI 只能引用這裡給的數字,禁止自己「記得」任何行情。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TICKERS = {"dxy": "DX-Y.NYB", "us10y": "^TNX", "vix": "^VIX"}


@dataclass
class CrossMarketData:
    dxy: float | None = None
    dxy_chg_pct: float | None = None       # 對前收盤漲跌 %
    us10y: float | None = None             # 殖利率(%)
    us10y_chg: float | None = None         # 對前收盤變化(百分點)
    vix: float | None = None
    fetched_at: str = ""
    ok: bool = False

    def to_prompt_dict(self) -> dict:
        """緊湊 JSON(餵給 AI;None 直接保留,AI 被要求不得腦補)。"""
        return {"dxy": self.dxy, "dxy_chg_pct": self.dxy_chg_pct,
                "us10y": self.us10y, "us10y_chg": self.us10y_chg, "vix": self.vix}

    def interpretation_zh(self) -> str:
        parts: list[str] = []
        if self.dxy_chg_pct is not None:
            parts.append(f"美元指數{'走強' if self.dxy_chg_pct > 0 else '走弱'}"
                         f"({self.dxy_chg_pct:+.2f}%),"
                         f"{'對黃金偏壓' if self.dxy_chg_pct > 0 else '對黃金偏撐'}")
        if self.us10y_chg is not None:
            parts.append(f"美十年債殖利率{'上行' if self.us10y_chg > 0 else '下行'}"
                         f"({self.us10y_chg:+.2f}),"
                         f"{'實質利率壓力增加' if self.us10y_chg > 0 else '利率壓力緩解'}")
        if self.vix is not None:
            mood = "恐慌情緒高" if self.vix >= 25 else "情緒偏謹慎" if self.vix >= 18 else "風險情緒平穩"
            parts.append(f"VIX {self.vix:.1f}({mood})")
        return ";".join(parts) if parts else "跨市場資料暫缺"


@dataclass
class _Cache:
    data: CrossMarketData = field(default_factory=CrossMarketData)
    fetched_at: datetime | None = None


_cache = _Cache()


def _fetch_sync() -> CrossMarketData:
    """同步抓取(在 thread 中執行,避免卡 event loop)。"""
    import yfinance as yf
    out = CrossMarketData(fetched_at=datetime.now(timezone.utc).isoformat())
    for key, ticker in _TICKERS.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d", interval="1d")
            if hist is None or hist.empty:
                continue
            last = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
            if key == "dxy":
                out.dxy = round(last, 2)
                if prev:
                    out.dxy_chg_pct = round((last - prev) / prev * 100, 2)
            elif key == "us10y":
                out.us10y = round(last, 2)
                if prev is not None:
                    out.us10y_chg = round(last - prev, 2)
            elif key == "vix":
                out.vix = round(last, 1)
        except Exception as exc:  # noqa: BLE001 — 單一 ticker 失敗不影響其他
            logger.warning("cross_market fetch %s failed: %s", ticker, exc)
    out.ok = any(v is not None for v in (out.dxy, out.us10y, out.vix))
    return out


async def get_cross_market() -> CrossMarketData:
    """取跨市場資料(快取優先;失敗回舊快取或空物件)。"""
    from app.config import get_settings
    s = get_settings()
    if s.mock_data_mode:            # 測試/開發不打外部網路
        return _cache.data
    now = datetime.now(timezone.utc)
    if (_cache.fetched_at is not None and
            (now - _cache.fetched_at).total_seconds() < s.cross_market_cache_minutes * 60):
        return _cache.data
    try:
        data = await asyncio.wait_for(asyncio.to_thread(_fetch_sync), timeout=20)
        if data.ok:
            _cache.data, _cache.fetched_at = data, now
            return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("cross_market fetch failed: %s", exc)
    # 失敗:回舊快取(可能為空)
    return _cache.data

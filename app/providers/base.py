"""Provider 抽象介面與共用重試(指數退避)工具。"""
from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class PriceTick:
    symbol: str
    bid: float
    ask: float
    quote_time: datetime          # UTC
    provider: str

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2, 3)

    @property
    def spread(self) -> float:
        return round(self.ask - self.bid, 3)


@dataclass
class Candle:
    symbol: str
    timeframe: str
    open_time: datetime           # UTC
    close_time: datetime          # UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0           # Tick Volume(不得冒充交易所成交量)
    bid_close: float | None = None
    ask_close: float | None = None
    spread: float | None = None
    is_closed: bool = False
    data_provider: str = ""
    extra: dict = field(default_factory=dict)


class ProviderError(Exception):
    """Provider 呼叫失敗(重試耗盡後拋出)。"""


class QuotaExceededError(ProviderError):
    """免費層配額用盡;呼叫端應降頻或改用其他來源。"""


async def with_retry(fn: Callable[[], Awaitable[T]], *, retries: int = 3,
                     base_delay: float = 1.0, max_delay: float = 30.0,
                     provider: str = "?") -> T:
    """指數退避重試(spec 二十七之6)。QuotaExceededError 不重試。"""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await fn()
        except QuotaExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 — 網路層錯誤種類多,統一退避
            last_exc = exc
            if attempt >= retries:
                break
            delay = min(max_delay, base_delay * (2 ** attempt)) * (1 + random.random() * 0.3)
            logger.warning("provider %s failed (attempt %d/%d): %s — retry in %.1fs",
                           provider, attempt + 1, retries, exc, delay)
            await asyncio.sleep(delay)
    raise ProviderError(f"{provider}: retries exhausted: {last_exc}") from last_exc


class MarketDataProvider(ABC):
    """行情 Provider 介面。所有時間欄位一律 UTC。"""

    name: str = "base"
    #: 是否僅供開發/回測(如 yfinance:延遲 + 期貨基差,禁止用於即時分析)
    realtime_capable: bool = True

    @abstractmethod
    async def get_live_price(self, symbol: str = "XAUUSD") -> PriceTick: ...

    @abstractmethod
    async def get_candles(self, symbol: str = "XAUUSD", timeframe: str = "15M",
                          count: int = 300) -> list[Candle]:
        """回傳按時間升冪的 K 棒;最後一根可能未收線(is_closed=False)。"""

    async def close(self) -> None:  # noqa: B027 — 預設無資源可釋放
        pass

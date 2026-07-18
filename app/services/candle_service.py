"""K 棒服務:抓取、儲存(upsert)、載入為 DataFrame、缺漏補齊。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select

from app.db.models import Candle as CandleRow
from app.db.session import db_session
from app.providers.base import Candle, MarketDataProvider
from app.utils.timeutils import NY, TIMEFRAME_MINUTES, ensure_utc, trading_day, trading_day_bounds

logger = logging.getLogger(__name__)


def candle_close_time(open_time: datetime, timeframe: str) -> datetime:
    """依週期推算 close_time(UTC)。日/週線依 NY 17:00 ET 規則(spec 三)。"""
    open_time = ensure_utc(open_time)
    if timeframe in TIMEFRAME_MINUTES:
        return open_time + timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    if timeframe == "1D":
        day = trading_day(open_time + timedelta(minutes=1))
        return trading_day_bounds(day)[1]
    if timeframe == "1W":
        ny = open_time.astimezone(NY)
        # 週線收於下週五 17:00 ET(自開盤起 5 個交易日)
        days_to_friday = (4 - ny.weekday()) % 7 or 7
        close_ny = (ny + timedelta(days=days_to_friday)).replace(hour=17, minute=0,
                                                                 second=0, microsecond=0)
        return close_ny.astimezone(timezone.utc)
    raise ValueError(f"unknown timeframe {timeframe}")


def filter_market_hours(candles: list[Candle],
                        holidays: frozenset | set = frozenset()) -> list[Candle]:
    """剔除休市時段的 K 棒(spec 四)。

    部分資料源(如 Twelve Data 的 XAU/USD)在週末/休市仍發布幾乎不動的
    「殭屍報價」;這些 K 棒會壓扁 ATR、污染布林通道與結構判定,必須在
    進入任何引擎前濾掉。
    """
    from app.utils.timeutils import is_market_open
    return [c for c in candles if is_market_open(c.open_time, holidays)]


def aggregate_candles(candles: list[Candle], target_tf: str) -> list[Candle]:
    """由低週期 K 棒本地聚合出高週期 K 棒(依 NY 17:00 ET 交易日切分)。

    用途:MT5 等券商來源的日/週線以「伺服器時區午夜」切分,與本系統的
    NY 17:00 規則可能不一致 —— 一律改由 1H(或更低)已收線資料本地聚合,
    保證 PDH/PDL/Pivot 與看圖一致(spec 三)。
    """
    if not candles:
        return []

    def bucket_key(c: Candle):
        if target_tf == "1D":
            return trading_day(c.open_time)
        if target_tf == "1W":
            d = trading_day(c.open_time)
            return d - timedelta(days=d.weekday())
        minutes = TIMEFRAME_MINUTES[target_tf]
        t = c.open_time
        return t - timedelta(minutes=(t.hour * 60 + t.minute) % minutes,
                             seconds=t.second, microseconds=t.microsecond)

    buckets: dict = {}
    order: list = []
    for c in sorted(candles, key=lambda x: x.open_time):
        key = bucket_key(c)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(c)

    now = datetime.now(timezone.utc)
    out: list[Candle] = []
    for key in order:
        group = buckets[key]
        first, last = group[0], group[-1]
        if target_tf == "1D":
            close_time = trading_day_bounds(key)[1]
        elif target_tf == "1W":
            close_time = trading_day_bounds(key + timedelta(days=4))[1]  # 週五收盤
        else:
            close_time = last.close_time
        out.append(Candle(
            symbol=first.symbol, timeframe=target_tf,
            open_time=first.open_time, close_time=close_time,
            open=first.open, high=max(c.high for c in group),
            low=min(c.low for c in group), close=last.close,
            volume=sum(c.volume for c in group),
            bid_close=last.bid_close, ask_close=last.ask_close, spread=last.spread,
            is_closed=all(c.is_closed for c in group) and close_time <= now,
            data_provider=first.data_provider,
        ))
    return out


def store_candles(candles: list[Candle]) -> int:
    """Upsert K 棒(以 symbol+timeframe+open_time+provider 唯一)。回傳寫入/更新筆數。"""
    if not candles:
        return 0
    n = 0
    now = datetime.now(timezone.utc)
    with db_session() as db:
        for c in candles:
            row = db.execute(select(CandleRow).where(
                CandleRow.symbol == c.symbol, CandleRow.timeframe == c.timeframe,
                CandleRow.open_time == c.open_time, CandleRow.data_provider == c.data_provider,
            )).scalar_one_or_none()
            if row is None:
                row = CandleRow(symbol=c.symbol, timeframe=c.timeframe,
                                open_time=c.open_time, close_time=c.close_time,
                                data_provider=c.data_provider, received_at=now,
                                open=c.open, high=c.high, low=c.low, close=c.close)
                db.add(row)
            elif row.is_closed and c.is_closed:
                continue  # 已收線資料不覆寫
            row.open, row.high, row.low, row.close = c.open, c.high, c.low, c.close
            row.volume = c.volume
            row.bid_close, row.ask_close, row.spread = c.bid_close, c.ask_close, c.spread
            row.is_closed = c.is_closed
            row.close_time = c.close_time
            row.received_at = now
            n += 1
    return n


async def refresh_candles(provider: MarketDataProvider, timeframes: tuple[str, ...],
                          count: int = 300, symbol: str = "XAUUSD") -> dict[str, list[Candle]]:
    """抓取並儲存;斷線/缺漏由 count 覆蓋範圍自動補齊(重抓即補)。"""
    out: dict[str, list[Candle]] = {}
    for tf in timeframes:
        candles = await provider.get_candles(symbol, tf, count)
        store_candles(candles)
        out[tf] = candles
    return out


def candles_to_df(candles: list[Candle], closed_only: bool = False) -> pd.DataFrame:
    """轉為分析用 DataFrame(index=open_time UTC)。

    closed_only=True 時僅保留已收線 K 棒 —— 結構/突破/交叉判定必須用此模式(spec 三)。
    """
    rows = [c for c in candles if c.is_closed] if closed_only else list(candles)
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume",
                                     "spread", "is_closed"])
    df = pd.DataFrame({
        "open": [c.open for c in rows], "high": [c.high for c in rows],
        "low": [c.low for c in rows], "close": [c.close for c in rows],
        "volume": [c.volume for c in rows], "spread": [c.spread for c in rows],
        "is_closed": [c.is_closed for c in rows],
    }, index=pd.DatetimeIndex([c.open_time for c in rows], name="open_time"))
    return df[~df.index.duplicated(keep="last")].sort_index()

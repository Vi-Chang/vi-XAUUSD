"""MT5 Provider(TMGM 等 MetaTrader 5 券商)— 經由本機已登入的 MT5 終端機取行情。

前置需求(Windows 專用):
1. 安裝 MetaTrader 5 終端機並登入 TMGM 帳戶(Demo 或 Live 皆可,行情唯讀)。
2. `pip install MetaTrader5`(官方套件,不在 requirements.txt 內,因無 Linux wheel,
   Docker 部署時此 Provider 不可用 → 改用 OANDA 或於 Windows 主機直跑)。
3. 預設「附掛」已登入的終端機,不需要在 .env 存帳密;
   也可設定 MT5_LOGIN / MT5_PASSWORD / MT5_SERVER 讓程式自行登入。

時區處理(關鍵):
- MT5 回傳的 K 棒時間是「伺服器時區的牆上時間」偽裝成 epoch 秒。
- TMGM 等 NY-close 對齊券商通常為冬令 UTC+2 / 夏令 UTC+3。
- 本 Provider 於行情新鮮時自動偵測時差(比對最新 tick 與本機 UTC),
  也可用 MT5_SERVER_UTC_OFFSET_HOURS 手動指定;偵測結果會記錄於 log。
- 日線/週線一律不用券商日線,改由 1H 已收線資料依 NY 17:00 ET 本地聚合
  (candle_service.aggregate_candles),徹底避開伺服器時區差異。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.providers.base import Candle, MarketDataProvider, PriceTick, ProviderError

logger = logging.getLogger(__name__)

#: 自動偵測時,tick 距今超過此秒數視為過期、不採用(避免週末用舊 tick 推錯時差)
_FRESH_TICK_MAX_AGE = 300


def infer_server_offset_hours(server_naive_epoch: float, utc_now: datetime) -> int:
    """由「伺服器牆上時間偽 epoch」與真實 UTC 推算伺服器時差(小時,四捨五入)。"""
    server_wall = datetime.fromtimestamp(server_naive_epoch, tz=timezone.utc)
    return round((server_wall - utc_now).total_seconds() / 3600)


class Mt5Provider(MarketDataProvider):
    name = "mt5_tmgm"
    realtime_capable = True

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError as exc:
            raise ProviderError(
                "MetaTrader5 套件未安裝。請在 Windows 主機執行:pip install MetaTrader5"
            ) from exc
        self._mt5 = mt5
        s = get_settings()
        self._symbol_broker = s.mt5_symbol

        kwargs: dict = {}
        if s.mt5_terminal_path:
            kwargs["path"] = s.mt5_terminal_path
        if s.mt5_login:
            kwargs.update(login=s.mt5_login, password=s.mt5_password, server=s.mt5_server)
        if not mt5.initialize(**kwargs):
            raise ProviderError(
                f"MT5 initialize 失敗:{mt5.last_error()}。"
                "請確認 MT5 終端機已安裝且已登入 TMGM 帳戶。")
        if not mt5.symbol_select(self._symbol_broker, True):
            raise ProviderError(
                f"MT5 找不到商品 {self._symbol_broker}(TMGM 帳戶類型不同後綴可能不同,"
                "請在終端機的市場報價視窗確認正確代碼後設定 MT5_SYMBOL)")

        self._tf_map = {"5M": mt5.TIMEFRAME_M5, "15M": mt5.TIMEFRAME_M15,
                        "30M": mt5.TIMEFRAME_M30, "1H": mt5.TIMEFRAME_H1,
                        "4H": mt5.TIMEFRAME_H4}
        self._offset_hours: int | None = s.mt5_server_utc_offset_hours
        self._detect_offset()
        acct = mt5.account_info()
        logger.info("MT5 connected: server=%s login=%s symbol=%s utc_offset=%+dh",
                    getattr(acct, "server", "?"), getattr(acct, "login", "?"),
                    self._symbol_broker, self._offset_hours or 0)

    # ── 時區 ──────────────────────────────────────────────
    def _detect_offset(self) -> None:
        """行情新鮮時自動偵測伺服器時差;否則沿用設定值/前次值。"""
        if self._offset_hours is not None and get_settings().mt5_server_utc_offset_hours is not None:
            return  # 使用者手動指定,不覆寫
        tick = self._mt5.symbol_info_tick(self._symbol_broker)
        if tick is None:
            return
        now = datetime.now(timezone.utc)
        inferred = infer_server_offset_hours(tick.time, now)
        # 用推得的時差還原 tick 真實 UTC,檢查新鮮度
        tick_utc = datetime.fromtimestamp(tick.time, tz=timezone.utc) - timedelta(hours=inferred)
        if abs((now - tick_utc).total_seconds()) <= _FRESH_TICK_MAX_AGE:
            if inferred != self._offset_hours:
                logger.info("MT5 server UTC offset detected: %+dh", inferred)
            self._offset_hours = inferred
        elif self._offset_hours is None:
            # 休市中無新鮮 tick:暫用 NY-close 券商慣例(美國夏令 +3 / 冬令 +2)
            from zoneinfo import ZoneInfo
            ny_dst = bool(now.astimezone(ZoneInfo("America/New_York")).dst())
            self._offset_hours = 3 if ny_dst else 2
            logger.warning("MT5 時差無法即時偵測(休市?),暫用慣例 %+dh;"
                           "開盤後將自動校正", self._offset_hours)

    def _to_utc(self, server_epoch: float) -> datetime:
        return (datetime.fromtimestamp(server_epoch, tz=timezone.utc)
                - timedelta(hours=self._offset_hours or 0))

    # ── 行情 ──────────────────────────────────────────────
    async def get_live_price(self, symbol: str = "XAUUSD") -> PriceTick:
        def _fetch() -> PriceTick:
            self._detect_offset()
            tick = self._mt5.symbol_info_tick(self._symbol_broker)
            if tick is None or tick.bid <= 0:
                raise ProviderError(f"MT5 無 {self._symbol_broker} 報價:{self._mt5.last_error()}")
            return PriceTick(symbol=symbol, bid=float(tick.bid), ask=float(tick.ask),
                            quote_time=self._to_utc(tick.time), provider=self.name)

        return await asyncio.to_thread(_fetch)

    async def get_candles(self, symbol: str = "XAUUSD", timeframe: str = "15M",
                          count: int = 300) -> list[Candle]:
        # 日/週線:由 1H 本地聚合(NY 17:00 ET 切分),不用券商日線
        if timeframe in ("1D", "1W"):
            hours_needed = count * 24 if timeframe == "1D" else count * 24 * 7
            h1 = await self.get_candles(symbol, "1H", min(hours_needed, 8000))
            from app.services.candle_service import aggregate_candles
            closed_h1 = [c for c in h1 if c.is_closed]
            if h1 and not h1[-1].is_closed:
                closed_h1.append(h1[-1])  # 保留最後一根未收線供盤中顯示
            agg = aggregate_candles(closed_h1, timeframe)
            return agg[-count:]

        def _fetch() -> list[Candle]:
            from app.services.candle_service import candle_close_time
            rates = self._mt5.copy_rates_from_pos(
                self._symbol_broker, self._tf_map[timeframe], 0, min(count, 8000))
            if rates is None or len(rates) == 0:
                raise ProviderError(
                    f"MT5 取 {self._symbol_broker} {timeframe} K 棒失敗:{self._mt5.last_error()}")
            info = self._mt5.symbol_info(self._symbol_broker)
            point_spread = (float(info.spread) * float(info.point)) if info else None
            now = datetime.now(timezone.utc)
            out: list[Candle] = []
            for r in rates:
                open_time = self._to_utc(float(r["time"]))
                close_time = candle_close_time(open_time, timeframe)
                out.append(Candle(
                    symbol=symbol, timeframe=timeframe,
                    open_time=open_time, close_time=close_time,
                    open=float(r["open"]), high=float(r["high"]),
                    low=float(r["low"]), close=float(r["close"]),
                    volume=float(r["tick_volume"]),  # Tick Volume(强制標記)
                    spread=point_spread,
                    is_closed=close_time <= now,
                    data_provider=self.name,
                ))
            return out

        return await asyncio.to_thread(_fetch)

    async def get_transactions(self, since: datetime | None = None) -> list[dict]:
        """TMGM 帳戶成交紀錄(Trading Coach 自動同步用;Phase 7 排程呼叫)。"""
        def _fetch() -> list[dict]:
            frm = since or (datetime.now(timezone.utc) - timedelta(days=30))
            deals = self._mt5.history_deals_get(frm, datetime.now(timezone.utc))
            if deals is None:
                return []
            return [d._asdict() for d in deals]

        return await asyncio.to_thread(_fetch)

    async def close(self) -> None:
        await asyncio.to_thread(self._mt5.shutdown)

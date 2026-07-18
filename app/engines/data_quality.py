"""Data Quality Engine(spec 四)。

每次分析前檢查 10 項;不符合 → NO_TRADE_DATA_QUALITY,禁止 AI 補值/猜值。
STALE 判定必須先查市場行事曆:休市期間不得誤報(spec 四之行事曆防呆)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

from app.config import get_settings
from app.providers.base import PriceTick
from app.utils.timeutils import expected_candle_open_times, is_market_open


@dataclass
class DataQualityReport:
    status: str = "GOOD"                      # GOOD/DEGRADED/STALE/FAILED
    missing_candles: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_mismatch: bool = False
    market_open: bool = True

    @property
    def tradeable(self) -> bool:
        return self.status in ("GOOD", "DEGRADED") and self.market_open


def check_candles(df: pd.DataFrame, timeframe: str,
                  holidays: frozenset[date] | set[date] = frozenset()) -> tuple[list[str], list[str]]:
    """缺漏/重複/順序檢查。回傳 (missing, warnings)。"""
    missing: list[str] = []
    warnings: list[str] = []
    if df.empty:
        return [f"{timeframe}: no data"], warnings
    idx = df.index
    if idx.duplicated().any():
        warnings.append(f"{timeframe}: duplicate candles x{int(idx.duplicated().sum())}")
    if not idx.is_monotonic_increasing:
        warnings.append(f"{timeframe}: timestamps not monotonic")
    if timeframe not in ("5M", "15M", "30M", "1H"):
        # 4H/1D/1W 的對齊方式因 provider(dailyAlignment)而異,
        # 缺漏檢查只對 UTC 整點對齊的盤中週期執行,避免誤報
        return missing, warnings
    expected = expected_candle_open_times(idx[0].to_pydatetime(), idx[-1].to_pydatetime(),
                                          timeframe, holidays)
    have = set(idx)
    for t in expected:
        ts = pd.Timestamp(t)
        if ts not in have:
            missing.append(f"{timeframe}: {t.isoformat()}")
    # OHLC 合理性
    bad = df[(df["high"] < df["low"]) | (df["high"] < df["close"]) | (df["low"] > df["close"])]
    if not bad.empty:
        warnings.append(f"{timeframe}: {len(bad)} candles with inconsistent OHLC")
    return missing, warnings


def check_live_price(tick: PriceTick | None, *, now: datetime | None = None,
                     holidays: frozenset[date] | set[date] = frozenset(),
                     spread_p95: float | None = None,
                     stale_after_seconds: int | None = None) -> tuple[list[str], bool]:
    """報價過期 / Bid<Ask / Spread 異常。回傳 (warnings, is_stale)。

    stale_after_seconds:低頻 provider(如 Twelve Data 5 分鐘輪詢)需放寬門檻,
    由呼叫端依實際輪詢間隔傳入;預設用 config.stale_price_seconds。
    """
    s = get_settings()
    threshold = stale_after_seconds or s.stale_price_seconds
    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []
    if tick is None:
        return ["no live price"], True
    if tick.bid > tick.ask:
        warnings.append(f"bid({tick.bid}) > ask({tick.ask})")
    age = (now - tick.quote_time).total_seconds()
    stale = False
    if age > threshold:
        if is_market_open(now, holidays):
            warnings.append(f"STALE_DATA: price age {age:.0f}s > {threshold}s")
            stale = True
        # 休市中不報 STALE(行事曆防呆)
    if spread_p95 is not None and tick.spread > spread_p95 * 3:
        warnings.append(f"abnormal spread {tick.spread} (p95={spread_p95})")
    return warnings, stale


def check_source_mismatch(primary_mid: float, secondary_mid: float | None,
                          atr15: float | None, *, event_window: bool = False) -> tuple[bool, str]:
    """主/備援價差(spec 四之 SOURCE_MISMATCH 修正門檻)。

    門檻 = max(source_mismatch_pct × price, source_mismatch_atr_mult × 15M ATR);
    高波動事件時段乘以放寬倍數。
    """
    s = get_settings()
    if secondary_mid is None:
        return False, ""
    threshold = max(s.source_mismatch_pct * primary_mid,
                    s.source_mismatch_atr_mult * (atr15 or 0.0))
    if event_window:
        threshold *= s.source_mismatch_event_relax_mult
    diff = abs(primary_mid - secondary_mid)
    if diff > threshold:
        return True, f"SOURCE_MISMATCH: |{primary_mid}-{secondary_mid}|={diff:.2f} > {threshold:.2f}"
    return False, ""


def evaluate(candles_by_tf: dict[str, pd.DataFrame], tick: PriceTick | None, *,
             secondary_mid: float | None = None, atr15: float | None = None,
             holidays: frozenset[date] | set[date] = frozenset(),
             now: datetime | None = None, event_window: bool = False,
             stale_after_seconds: int | None = None) -> DataQualityReport:
    """整合評估 → GOOD / DEGRADED / STALE / FAILED。"""
    now = now or datetime.now(timezone.utc)
    report = DataQualityReport(market_open=is_market_open(now, holidays))

    for tf, df in candles_by_tf.items():
        missing, warns = check_candles(df, tf, holidays)
        report.missing_candles.extend(missing)
        report.warnings.extend(warns)

    price_warns, stale = check_live_price(tick, now=now, holidays=holidays,
                                          stale_after_seconds=stale_after_seconds)
    report.warnings.extend(price_warns)

    if tick is not None:
        mismatch, msg = check_source_mismatch(tick.mid, secondary_mid, atr15,
                                              event_window=event_window)
        report.source_mismatch = mismatch
        if msg:
            report.warnings.append(msg)

    required_missing = any(not df.empty for df in candles_by_tf.values())
    if tick is None or all(df.empty for df in candles_by_tf.values()):
        report.status = "FAILED"
    elif stale:
        report.status = "STALE"
    elif report.missing_candles or report.source_mismatch or not required_missing:
        report.status = "DEGRADED"
    elif any("bid(" in w or "inconsistent OHLC" in w for w in report.warnings):
        report.status = "DEGRADED"
    else:
        report.status = "GOOD"
    return report

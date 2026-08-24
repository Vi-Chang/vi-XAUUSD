"""Canonical, timezone-safe access to the latest genuinely closed candle."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from app.providers.base import Candle

TIMEFRAME_MINUTES = {"1M": 1, "5M": 5, "15M": 15, "30M": 30,
                     "1H": 60, "4H": 240, "1D": 1440}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ClosedCandleSnapshot:
    timeframe: str
    open_time: str | None
    close_time: str | None
    close_price: float | None
    is_closed: bool
    available: bool
    error_reason: str | None = None
    source: str | None = None

    def model_dump(self) -> dict:
        return asdict(self)


class ClosedCandleService:
    """The sole boundary that decides whether a candle is closed.

    Provider timestamps remain UTC.  Presentation converts them to local time;
    no engine may infer the prior bar from ``now - timeframe``.
    """

    @staticmethod
    def latest(candles: Iterable[Candle], *, timeframe: str,
               decision_time: datetime) -> ClosedCandleSnapshot:
        now = _utc(decision_time)
        candidates: list[Candle] = []
        invalid_timestamp = False
        for candle in candles:
            try:
                open_time, close_time = _utc(candle.open_time), _utc(candle.close_time)
            except (TypeError, ValueError):
                invalid_timestamp = True
                continue
            if candle.is_closed and close_time <= now:
                candidates.append(candle)
        if not candidates:
            reason = "PARSE_ERROR" if invalid_timestamp else "DATA_GAP"
            return ClosedCandleSnapshot(timeframe, None, None, None, False,
                                        False, reason)
        candle = max(candidates, key=lambda item: _utc(item.close_time))
        open_time, close_time = _utc(candle.open_time), _utc(candle.close_time)
        minutes = TIMEFRAME_MINUTES.get(timeframe.upper())
        if minutes and close_time - open_time != timedelta(minutes=minutes):
            return ClosedCandleSnapshot(
                timeframe, open_time.isoformat(), close_time.isoformat(),
                float(candle.close), True, False, "TIMESTAMP_MISMATCH",
                candle.data_provider or None)
        return ClosedCandleSnapshot(
            timeframe, open_time.isoformat(), close_time.isoformat(),
            float(candle.close), True, True, None,
            candle.data_provider or None)


def canonical_closed_candles(candles_by_timeframe: dict[str, list[Candle]], *,
                             decision_time: datetime) -> dict[str, dict]:
    return {timeframe: ClosedCandleService.latest(
        rows, timeframe=timeframe, decision_time=decision_time).model_dump()
        for timeframe, rows in candles_by_timeframe.items()}

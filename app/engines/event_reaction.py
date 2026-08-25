"""Conservative post-event confirmation for the trading analysis."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EventReaction:
    status: str = "not_applicable"
    xauusd_confirmation: str = "not_checked"
    dxy_confirmation: str = "not_available"
    yield_confirmation: str = "not_available"
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None
    outcome_status: str = "not_available"
    outcome_source: str = ""
    surprise: float | None = None
    fundamental_bias: str = "unknown"
    message: str = ""


def assess_event_reaction(*, post_event_wait: bool, m15_closed_at: str,
                          macd_hist: float | None, dxy_chg_pct: float | None,
                          us10y_chg: float | None, actual: float | None = None,
                          forecast: float | None = None, previous: float | None = None,
                          outcome_source: str = "", event_name: str = "") -> EventReaction:
    """Separate provider-supplied macro results from technical confirmation.

    The function deliberately never infers actual or forecast numbers from a price
    move.  A missing result means the analysis remains technically confirmed only.
    """
    outcome_status = ("available" if actual is not None and forecast is not None
                      else "pending" if forecast is not None or previous is not None
                      else "not_available")
    surprise = (round(actual - forecast, 6)
                if actual is not None and forecast is not None else None)
    stronger_us = any(term in event_name.lower() for term in
                      ("cpi", "ppi", "pce", "employment", "payroll", "gdp", "fomc"))
    fundamental_bias = ("bearish_xauusd" if surprise is not None and surprise > 0 and stronger_us
                        else "bullish_xauusd" if surprise is not None and surprise < 0 and stronger_us
                        else "neutral" if surprise == 0 else "unknown")
    common: dict[str, Any] = {
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "outcome_status": outcome_status,
        "outcome_source": outcome_source,
        "surprise": surprise,
        "fundamental_bias": fundamental_bias,
    }
    if not post_event_wait:
        return EventReaction(**common, message="目前沒有進入事件公布後確認窗口。")
    if not m15_closed_at:
        return EventReaction(**common, status="awaiting_close", xauusd_confirmation="awaiting_close",
                             message="事件後等待第一根 15 分鐘 K 棒收盤確認。")

    xauusd = "bullish" if (macd_hist or 0) > 0 else "bearish" if (macd_hist or 0) < 0 else "neutral"
    dxy = "available" if dxy_chg_pct is not None else "not_available"
    yields = "available" if us10y_chg is not None else "not_available"
    available = sum(value != "not_available" for value in (dxy, yields))
    status = "confirmed" if available == 2 else "mixed"
    message = ("事件後 15 分鐘 K 棒已收盤，跨市場資料可用，等待價格結構確認後再評估。"
               if status == "confirmed" else
               "事件後 15 分鐘 K 棒已收盤，但美元或美債資料不足；維持等待確認。")
    if outcome_status != "available":
        message += " 事件實際值或預期值尚未由資料來源提供；目前僅確認技術面反應。"
    return EventReaction(**common, status=status, xauusd_confirmation=xauusd,
                         dxy_confirmation=dxy, yield_confirmation=yields, message=message)

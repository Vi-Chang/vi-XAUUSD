"""公布後只看可驗證的市場反應；不把新聞的理論方向當成交易指令。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventReaction:
    status: str = "not_applicable"
    xauusd_confirmation: str = "not_checked"
    dxy_confirmation: str = "not_available"
    yield_confirmation: str = "not_available"
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None
    message: str = ""


def assess_event_reaction(*, post_event_wait: bool, m15_closed_at: str,
                          macd_hist: float | None, dxy_chg_pct: float | None,
                          us10y_chg: float | None) -> EventReaction:
    """事件窗內一律等已收盤 15 分K；資料不足則維持等待而不是猜方向。"""
    if not post_event_wait:
        return EventReaction(message="目前不在高影響事件發布後確認期。")
    if not m15_closed_at:
        return EventReaction(status="awaiting_close", xauusd_confirmation="awaiting_close",
                             message="高影響事件已發布；尚無可用的已收盤 15 分K，等待確認。")
    xauusd = "bullish" if (macd_hist or 0) > 0 else "bearish" if (macd_hist or 0) < 0 else "neutral"
    dxy = "available" if dxy_chg_pct is not None else "not_available"
    yields = "available" if us10y_chg is not None else "not_available"
    available = sum(value != "not_available" for value in (dxy, yields))
    status = "confirmed" if available == 2 else "mixed"
    message = ("已取得發布後 15 分K與美元、殖利率確認資料；仍須以價格結構與進場條件決定。"
               if status == "confirmed" else
               "已取得發布後 15 分K，但跨市場資料不完整；維持等待，不以新聞方向直接進場。")
    return EventReaction(status=status, xauusd_confirmation=xauusd,
                         dxy_confirmation=dxy, yield_confirmation=yields, message=message)

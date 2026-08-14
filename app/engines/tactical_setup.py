"""No-lookahead tactical setup state derived from closed 15M structure."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

SetupState = Literal[
    "OBSERVE", "LONG_WATCH", "SHORT_WATCH", "LONG_READY", "SHORT_READY", "NO_CHASE"
]
TacticalBias = Literal["bullish", "bearish", "neutral"]


@dataclass(frozen=True)
class TacticalSetup:
    tactical_bias: TacticalBias = "neutral"
    setup_state: SetupState = "OBSERVE"
    trigger_level: float | None = None
    invalidation_level: float | None = None
    expires_at: str = ""
    next_check_time: str = ""
    missing_condition: str = "等待已收盤 15 分 K 確認方向。"
    message: str = "目前觀望；等待已收盤 15 分 K 確認方向。"


def classify_tactical_setup(*, support_state: str, weakness_state: str,
                            weakness_families: list[str], trend_bias: str,
                            current_price: float, support: float | None,
                            buffer: float, atr15: float,
                            last_closed_at: str, rr_to_next_support: float | None,
                            bullish_breakout_active: bool = False,
                            retest_failed: bool = False,
                            chase_atr_mult: float = 1.5,
                            min_rr: float = 1.5,
                            expiry_bars: int = 4) -> TacticalSetup:
    """Classify using only state known at ``last_closed_at``."""
    expires = ""
    next_check = ""
    if last_closed_at:
        try:
            stamp = datetime.fromisoformat(last_closed_at)
            next_check = (stamp + timedelta(minutes=15)).isoformat()
            expires = (stamp + timedelta(minutes=15 * max(1, expiry_bars))).isoformat()
        except ValueError:
            expires = ""
    invalidation = support + buffer if support is not None else None
    bearish_families = set(weakness_families) & {
        "price_structure", "momentum", "oscillator",
        "higher_timeframe_momentum", "volatility",
    }
    confirmed_structure = support_state in ("confirmed_breakdown", "retest_rejected")
    confirmed_momentum = weakness_state in ("confirmed", "accelerating") \
        and len(bearish_families) >= 2
    bearish_confirmed = confirmed_structure and confirmed_momentum \
        and not bullish_breakout_active and support is not None

    if not bearish_confirmed:
        missing = []
        if not confirmed_structure:
            missing.append("第二根已收盤 15 分 K 未確認支撐失守")
        if not confirmed_momentum:
            missing.append("空方獨立證據未滿兩個家族")
        if bullish_breakout_active:
            missing.append("多方突破證據尚未失效")
        detail = "；".join(missing) or "方向證據尚未一致"
        return TacticalSetup(
            trigger_level=support, invalidation_level=invalidation, expires_at=expires,
            next_check_time=next_check,
            missing_condition=detail, message=f"目前觀望；缺少：{detail}。")

    distance = max(0.0, (support or current_price) - current_price)
    if atr15 > 0 and distance > atr15 * chase_atr_mult:
        return TacticalSetup(
            tactical_bias="bearish", setup_state="NO_CHASE",
            trigger_level=support, invalidation_level=invalidation, expires_at=expires,
            next_check_time=next_check,
            missing_condition="價格已遠離跌破位，等待反彈回測失敗",
            message=(f"空方方向成立，但目前不追空；等待反彈至 {support:.2f} "
                     "附近失敗再評估。"))

    rr_ok = rr_to_next_support is not None and rr_to_next_support >= min_rr
    if (support_state == "retest_rejected" or retest_failed) and rr_ok:
        return TacticalSetup(
            tactical_bias="bearish", setup_state="SHORT_READY",
            trigger_level=support, invalidation_level=invalidation, expires_at=expires,
            next_check_time=next_check,
            missing_condition="無；回測失敗且盈虧比達標",
            message=(f"15M 跌破後回測 {support:.2f} 失敗，空方條件完成；"
                     f"失效價 {invalidation:.2f}。"))

    missing = ("等待反彈回測失敗" if not retest_failed
               else f"至下一支撐的盈虧比尚未達 {min_rr:.1f}")
    return TacticalSetup(
        tactical_bias="bearish", setup_state="SHORT_WATCH",
        trigger_level=support, invalidation_level=invalidation, expires_at=expires,
        next_check_time=next_check,
        missing_condition=missing,
        message=(f"15M 空方結構已確認；{missing}。"
                 + ("4H/1H 仍偏多，僅降低信心，不否決短空。"
                    if trend_bias == "bullish" else "")))

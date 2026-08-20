"""持倉風險優先覆寫：純函式、只使用已計算的結構化資料。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.schemas.analysis import AssessmentReason, InvalidationCondition

Weakness = Literal["none", "early_warning", "confirmed", "accelerating"]


@dataclass
class WeaknessResult:
    state: Weakness = "none"
    families: list[str] = field(default_factory=list)
    oversold: bool = False
    recovery_candidate: bool = False
    reasons: list[str] = field(default_factory=list)


def detect_short_term_weakness(*, indicators: dict, support_state: str) -> WeaknessResult:
    """整合結構、MACD、RSI/KD 與 1H 延續性；同家族最多算一票。"""
    m15, h1 = indicators.get("15M", {}), indicators.get("1H", {})
    families: list[str] = []
    reasons: list[str] = []

    structure_broken = support_state in ("confirmed_breakdown", "retest_rejected")
    structure_recovered = support_state == "failed_breakdown"
    if structure_broken:
        families.append("price_structure")
        reasons.append("15M 已收盤跌破動態支撐或反抽無法站回")

    hist = m15.get("macd_hist")
    hist_prev = m15.get("macd_hist_prev")
    macd_negative = hist is not None and hist < 0
    macd_expanding = macd_negative and hist_prev is not None and hist_prev < 0 and hist < hist_prev
    macd_contracting = macd_negative and hist_prev is not None and hist > hist_prev
    if macd_negative:
        families.append("momentum")
        reasons.append("15M MACD 柱位於零軸下方" + ("且負值擴大" if macd_expanding else ""))

    rsi_fast = m15.get("rsi6", m15.get("rsi14"))
    rsi_slow = m15.get("rsi12", m15.get("rsi14"))
    rsi_fast_prev = m15.get("rsi6_prev", m15.get("rsi14_prev"))
    k, d = m15.get("stoch_k"), m15.get("stoch_d")
    k_prev = m15.get("stoch_k_prev")
    rsi_falling = (rsi_fast is not None and rsi_fast_prev is not None
                   and rsi_fast < rsi_fast_prev)
    oscillator_bearish = ((rsi_slow is not None and rsi_slow < 50 and rsi_falling)
                          or (k is not None and d is not None and k < d
                              and (k_prev is None or k < k_prev)))
    oversold = ((rsi_fast is not None and rsi_fast < 30)
                or (k is not None and k < 20))
    if oscillator_bearish:
        families.append("oscillator")
        reasons.append("15M RSI／KD 家族仍在下行" + ("（已超賣但尚未止跌）" if oversold else ""))

    h1_hist, h1_prev, h1_prev2 = (h1.get("macd_hist"), h1.get("macd_hist_prev"),
                                  h1.get("macd_hist_prev2"))
    h1_cooling = (h1_hist is not None and h1_prev is not None and h1_hist < h1_prev
                  and (h1_prev2 is None or h1_prev < h1_prev2))
    h1_rsi = h1.get("rsi14")
    if h1_cooling or (h1_rsi is not None and h1_rsi < 50):
        families.append("higher_timeframe_momentum")
        reasons.append("1H 動能連續衰退或失去 RSI 中軸")

    families = list(dict.fromkeys(families))
    if structure_broken and macd_expanding and len(families) >= 3:
        state: Weakness = "accelerating"
    elif len(families) >= 2 and (structure_broken or macd_negative):
        state = "confirmed"
    # RSI/KD alone (especially overbought) is location context, not proof that
    # price structure has weakened. Require structure or MACD/1H momentum.
    elif structure_broken or macd_negative or h1_cooling:
        state = "early_warning"
    else:
        state = "none"

    # 超賣後只能成為「恢復候選」，不能直接開啟多單。
    recovery = bool(structure_recovered and macd_contracting and not structure_broken)
    if recovery:
        reasons.append("價格站回結構位且負動能縮小，僅列為止跌候選，仍待下一根確認")
    return WeaknessResult(state=state, families=families, oversold=oversold,
                          recovery_candidate=recovery, reasons=reasons)


def apply_risk_priority(*, weakness: WeaknessResult, market_status: str,
                        event_status: str, event_lockout: bool,
                        market_regime: str, entry_readiness: str,
                        support_state: str, levels: list) -> dict:
    """固定優先序：資料→事件→持倉→短線→進場→趨勢。"""
    reasons: list[AssessmentReason] = []
    invalidations: list[InvalidationCondition] = []
    override = "none"
    position_risk = "normal"
    readiness = entry_readiness

    if market_status in ("STALE", "FAILED"):
        override, readiness = "suspend_all_entries", "no_trade"
        reasons.append(AssessmentReason(code="MARKET_DATA_INVALID", priority=1,
            message="行情資料失效或過期，暫停所有新進場。", evidenceFamilies=["data"]))
    elif event_lockout:
        override, readiness = "suspend_all_entries", "no_trade"
        reasons.append(AssessmentReason(code="EVENT_LOCKOUT", priority=2,
            message="高影響事件進入鎖定窗口，暫停所有新進場。", evidenceFamilies=["event"]))
    elif weakness.recovery_candidate:
        override, position_risk, readiness = "block_new_long", "elevated", "wait_confirmation"
    elif weakness.state == "accelerating":
        override, position_risk, readiness = "protect_existing_long", "elevated", "no_trade"
    elif weakness.state == "confirmed":
        override, position_risk, readiness = "protect_existing_long", "elevated", "wait_confirmation"
    elif weakness.state == "early_warning":
        override, position_risk, readiness = "block_new_long", "elevated", "wait_confirmation"

    if weakness.state != "none":
        reasons.append(AssessmentReason(code=f"SHORT_WEAKNESS_{weakness.state.upper()}",
            priority=3, message="；".join(weakness.reasons), evidenceFamilies=weakness.families))
    if event_status in ("STALE", "FAILED"):
        reasons.append(AssessmentReason(code="EVENT_DATA_UNKNOWN", priority=2,
            message="目前僅依技術面判斷，事件風險未納入。", evidenceFamilies=["event"]))

    for level in levels:
        invalidations.append(InvalidationCondition(
            code=f"STRUCTURE_{level.kind.upper()}", timeframe=level.timeframe,
            price=level.price, source=level.source,
            message=(f"{level.timeframe} {level.kind} {level.price:.2f}，"
                     f"緩衝 {level.buffer:.2f}；須由已收盤 K 棒確認。")))

    long_allowed = readiness == "ready" and override not in (
        "block_new_long", "protect_existing_long", "suspend_all_entries")
    short_allowed = readiness == "ready" and market_regime in ("bearish", "strong_bearish") \
        and override not in ("block_new_short", "protect_existing_short", "suspend_all_entries")
    long_guidance = ("短線空方動能擴大；請檢查原始停損、倉位大小及結構失效條件。"
                     "這是風險提醒，不代表已產生立即平倉訊號。"
                     if weakness.state == "accelerating" else
                     "短線已轉弱；觀察結構失效與收復狀態，不以短線指標直接判定平倉。"
                     if weakness.state in ("confirmed", "early_warning") else
                     "若已有多單，依原交易計畫管理；大週期偏多不等於保證可續抱。")
    short_guidance = ("若已有空單，依已收盤結構管理，不因超賣單獨平倉或反手。"
                      if weakness.state != "none" else
                      "若已有空單，留意大週期方向與結構失效條件。")
    return {"riskOverride": override, "positionRisk": position_risk,
            "entryReadiness": readiness, "longEntryAllowed": long_allowed,
            "shortEntryAllowed": short_allowed, "reasons": reasons,
            "invalidationConditions": invalidations,
            "existingLongGuidance": long_guidance,
            "existingShortGuidance": short_guidance}

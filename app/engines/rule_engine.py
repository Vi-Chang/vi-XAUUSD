"""規則引擎(spec 十四、十五、二十一)— 確定性輸出 WATCH / PREPARE / NO_TRADE。

- 訊號至少三項條件且必含價格結構確認(spec 十四)。
- 追價偵測:CHASE_LONG_RISK / CHASE_SHORT_RISK(spec 十五,ATR 倍數可調)。
- evidence_score 只能由明確條件加總(Python 計算),AI 不得主觀生成(spec 二十一)。
- 劇本價位一律引用候選價位 ID(spec 八)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import get_settings
from app.engines.data_quality import DataQualityReport
from app.engines.key_levels import CandidateLevel, nearest_zone
from app.engines.market_structure import StructureReport
from app.schemas.analysis import Scenario


@dataclass
class RuleDecision:
    action: str                       # NO_TRADE / WATCH / PREPARE_LONG / PREPARE_SHORT
    reason: str
    evidence_score: int
    confidence_grade: str             # S/A/B/C/X
    long_scenario: Scenario
    short_scenario: Scenario
    chase_flags: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)     # 引用 K 棒時間/事件/候選 ID
    no_trade_code: str | None = None  # NO_TRADE_DATA_QUALITY / NO_TRADE_MID_RANGE / ...


def _direction_conditions(direction: str, *, structures: dict[str, StructureReport],
                          indicators_h1: dict, price: float,
                          levels: list[CandidateLevel], atr15: float) -> list[str]:
    """收集某方向已成立的條件(每項附證據);結構條件在前。"""
    ok: list[str] = []
    m15, h1, h4 = structures.get("15M"), structures.get("1H"), structures.get("4H")
    up = direction == "LONG"

    # 結構條件(必要類)
    if m15:
        labels = [s.label for s in m15.swings if s.label][-3:]
        if up and "HL" in labels:
            ok.append("STRUCT:15M 已形成更高低點(HL)")
        if not up and "LH" in labels:
            ok.append("STRUCT:15M 已形成更低高點(LH)")
        for ev in m15.events[-4:]:
            if not ev.still_valid or ev.provisional:
                continue
            if up and ev.event_type in ("BOS_UP", "CHOCH_UP"):
                ok.append(f"STRUCT:15M 已收線突破局部高點 {ev.price:.2f} @ {ev.time}")
            if not up and ev.event_type in ("BOS_DOWN", "CHOCH_DOWN"):
                ok.append(f"STRUCT:15M 已收線跌破局部低點 {ev.price:.2f} @ {ev.time}")
            if up and ev.event_type == "FAILED_BREAKDOWN":
                ok.append(f"STRUCT:掃過前低後收回(假跌破)@ {ev.time}")
            if not up and ev.event_type == "FAILED_BREAKOUT":
                ok.append(f"STRUCT:掃過前高後跌回(假突破)@ {ev.time}")

    # 位置條件:靠近支撐做多 / 靠近壓力做空
    zone_kind = "SUP_ZONE" if up else "RES_ZONE"
    zone = nearest_zone(levels, price, zone_kind, "STRONG")
    if zone and zone.price_low - atr15 <= price <= zone.price_high + atr15:
        ok.append(f"LEVEL:價格位於{'支撐' if up else '壓力'}區 {zone.level_id} 附近")

    # 動能條件(1H)
    hist = indicators_h1.get("macd_hist")
    if hist is not None:
        if up and hist > 0:
            ok.append("MOMO:1H MACD 柱體位於零軸上方(動能偏多)")
        if not up and hist < 0:
            ok.append("MOMO:1H MACD 柱體位於零軸下方(動能偏空)")

    # 高週期不否決
    if h4:
        if up and h4.trend in ("UP", "RANGE", "UNKNOWN"):
            ok.append(f"HTF:4H 結構({h4.trend})支持或未否決多方")
        if not up and h4.trend in ("DOWN", "RANGE", "UNKNOWN"):
            ok.append(f"HTF:4H 結構({h4.trend})支持或未否決空方")
    if h1:
        if up and h1.trend == "UP":
            ok.append("HTF:1H 趨勢向上")
        if not up and h1.trend == "DOWN":
            ok.append("HTF:1H 趨勢向下")
    return ok


def detect_chase(direction: str, *, price: float, atr15: float,
                 structures: dict[str, StructureReport],
                 levels: list[CandidateLevel]) -> list[str]:
    """追價偵測(spec 十五)。"""
    s = get_settings()
    flags: list[str] = []
    m15 = structures.get("15M")
    up = direction == "LONG"

    if m15:
        ref = m15.last_swing_low if up else m15.last_swing_high
        if ref is not None and abs(price - ref) > s.chase_atr_mult * atr15:
            flags.append(f"CHASE_{'LONG' if up else 'SHORT'}_RISK:距最近 15M 結構點 "
                         f"{abs(price - ref) / max(atr15, 1e-9):.1f} ATR(>{s.chase_atr_mult})")

    # 距強壓力太近禁止追多;距強支撐太近禁止追空
    guard = nearest_zone(levels, price, "RES_ZONE" if up else "SUP_ZONE", "STRONG")
    if guard:
        edge = guard.price_low if up else guard.price_high
        dist = (edge - price) if up else (price - edge)
        if 0 <= dist < s.no_chase_near_level_atr_mult * atr15:
            flags.append(f"CHASE_{'LONG' if up else 'SHORT'}_RISK:距強"
                         f"{'壓力' if up else '支撐'} {guard.level_id} 僅 "
                         f"{dist / max(atr15, 1e-9):.2f} ATR,禁止追{'多' if up else '空'}")
    return flags


def _build_scenario(direction: str, conditions: list[str], *, price: float,
                    levels: list[CandidateLevel], atr15: float,
                    structures: dict[str, StructureReport]) -> tuple[Scenario, list[float]]:
    """組劇本(欄位全為候選 ID)並由 Python 計算 R/R(spec 十六)。"""
    up = direction == "LONG"
    entry = nearest_zone(levels, price, "SUP_ZONE" if up else "RES_ZONE", "STRONG")
    stop_ref = None
    m15 = structures.get("15M")
    for lv in levels:
        if up and lv.kind == "SWING_LOW_15M":
            stop_ref = lv
        if not up and lv.kind == "SWING_HIGH_15M":
            stop_ref = lv
    targets = [lv for lv in levels
               if (lv.kind == ("RES_ZONE" if up else "SUP_ZONE"))]
    targets.sort(key=lambda lv: lv.mid, reverse=not up)
    # 目標必須在進場的獲利方向
    if entry:
        targets = [t for t in targets if (t.mid > entry.mid if up else t.mid < entry.mid)]
        targets.sort(key=lambda lv: abs(lv.mid - entry.mid))
    targets = targets[:3]

    rr: list[float] = []
    if entry and stop_ref:
        entry_px = entry.mid
        stop_px = stop_ref.mid - (0.25 * atr15 if up else -0.25 * atr15)
        risk = abs(entry_px - stop_px)
        if risk > 0:
            rr = [round(abs(t.mid - entry_px) / risk, 2) for t in targets]

    structure_confirmed = any(c.startswith("STRUCT") for c in conditions)
    n = len(conditions)
    if n >= 3 and structure_confirmed:
        status = "PREPARE"
    else:
        status = "WATCH"

    scenario = Scenario(
        status=status,
        setup=f"{'多方' if up else '空方'}:{'; '.join(conditions[:3]) if conditions else '條件未成立,等待'}",
        entry_zone_id=entry.level_id if entry else None,
        required_confirmations=(
            [] if status == "PREPARE" else
            [f"等待 15M 已收線{'突破局部高點/形成 HL' if up else '跌破局部低點/形成 LH'}",
             "等待價格回到理想進場區(非追價位置)"]),
        stop_loss_id=stop_ref.level_id if stop_ref else None,
        target_ids=[t.level_id for t in targets],
        risk_reward=rr,
        invalidation_id=stop_ref.level_id if stop_ref else None,
    )
    return scenario, rr


def decide(*, quality: DataQualityReport, structures: dict[str, StructureReport],
           indicators_h1: dict, market_state: str, price: float, atr15: float,
           levels: list[CandidateLevel], event_lockout: bool = False) -> RuleDecision:
    """主決策。硬性風控(資料品質、事件鎖定)由此層強制執行,AI 無權推翻(spec 十三 D)。"""
    s = get_settings()
    empty_long, empty_short = Scenario(), Scenario()

    # 硬性否決
    if not quality.tradeable:
        code = "NO_TRADE_DATA_QUALITY" if quality.status in ("STALE", "FAILED", "DEGRADED") else "NO_TRADE_MARKET_CLOSED"
        if not quality.market_open:
            code = "NO_TRADE_MARKET_CLOSED"
        return RuleDecision("NO_TRADE", f"{code}: {quality.status}; {quality.warnings[:3]}",
                            0, "X", empty_long, empty_short, no_trade_code=code)
    if event_lockout:
        return RuleDecision("NO_TRADE", f"EVENT_LOCKOUT:高影響事件前 {s.event_lockout_minutes} 分鐘內禁止新倉",
                            0, "X", empty_long, empty_short, no_trade_code="EVENT_LOCKOUT")
    if market_state == "INSUFFICIENT_DATA":
        return RuleDecision("NO_TRADE", "資料不足,禁止強迫產生訊號(spec 老問題 19)",
                            0, "X", empty_long, empty_short, no_trade_code="NO_TRADE_DATA_QUALITY")

    long_conds = _direction_conditions("LONG", structures=structures,
                                       indicators_h1=indicators_h1, price=price,
                                       levels=levels, atr15=atr15)
    short_conds = _direction_conditions("SHORT", structures=structures,
                                        indicators_h1=indicators_h1, price=price,
                                        levels=levels, atr15=atr15)
    long_sc, long_rr = _build_scenario("LONG", long_conds, price=price, levels=levels,
                                       atr15=atr15, structures=structures)
    short_sc, short_rr = _build_scenario("SHORT", short_conds, price=price, levels=levels,
                                         atr15=atr15, structures=structures)
    chase = detect_chase("LONG", price=price, atr15=atr15, structures=structures, levels=levels) \
        + detect_chase("SHORT", price=price, atr15=atr15, structures=structures, levels=levels)

    # evidence_score:明確條件加總(每條件 10 分,上限 100)
    n_long, n_short = len(long_conds), len(short_conds)
    dominant = "LONG" if n_long > n_short else ("SHORT" if n_short > n_long else None)
    score = min(100, 10 * max(n_long, n_short)
                + (10 if quality.status == "GOOD" else 0)
                + (10 if not chase else 0))

    # R/R 檢核(spec 十六:主要目標原則上 >= 2R,否則等待)
    rr = long_rr if dominant == "LONG" else short_rr if dominant == "SHORT" else []
    rr_ok = bool(rr) and (rr[0] >= 1.0) and (max(rr) >= 2.0)

    if dominant is None or max(n_long, n_short) < 2 or market_state in ("RANGE", "COMPRESSION"):
        action, grade = "WATCH", "C"
        reason = f"市場狀態 {market_state};多方條件 {n_long} 項、空方條件 {n_short} 項,無明確優勢 → 等待"
    else:
        sc = long_sc if dominant == "LONG" else short_sc
        chase_this = [f for f in chase if dominant in f]
        if sc.status == "PREPARE" and rr_ok and not chase_this:
            action = f"PREPARE_{dominant}"
            grade = "A" if score >= 60 else "B"
            reason = (f"{dominant} 條件 {max(n_long, n_short)} 項成立(含結構確認),"
                      f"R/R={rr},等待最終觸發")
        elif sc.status == "PREPARE" and not rr_ok:
            action, grade = "WATCH", "C"
            reason = f"{dominant} 結構條件成立但 R/R 不足({rr or 'n/a'}),優先等待更好位置(spec 十六)"
        elif chase_this:
            action, grade = "WATCH", "C"
            reason = f"{dominant} 有方向但屬追價位置:{chase_this[0]}"
        else:
            action, grade = "WATCH", "B" if market_state.startswith("STRONG") else "C"
            reason = f"{dominant} 方向條件 {max(n_long, n_short)} 項,尚缺結構/收線確認"

    return RuleDecision(action=action, reason=reason, evidence_score=score,
                        confidence_grade=grade, long_scenario=long_sc,
                        short_scenario=short_sc, chase_flags=chase,
                        evidence=long_conds + short_conds)

"""規則引擎(spec 十四、十五、二十一)— 確定性輸出 WATCH / PREPARE / NO_TRADE。

- 訊號至少三項條件且必含價格結構確認(spec 十四)。
- 追價偵測:CHASE_LONG_RISK / CHASE_SHORT_RISK(spec 十五,ATR 倍數可調)。
- evidence_score 只能由明確條件加總(Python 計算),AI 不得主觀生成(spec 二十一)。
- 劇本價位一律引用候選價位 ID(spec 八)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import get_settings
from app.i18n import dir_zh, state_zh
from app.engines.data_quality import DataQualityReport
from app.engines.key_levels import CandidateLevel, nearest_zone
from app.engines.market_structure import StructureReport
from app.schemas.analysis import Scenario

logger = logging.getLogger(__name__)


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
    # 多空「證據傾向」(非勝率,spec 二十一):由已成立條件加權計算,結構條件 ×2
    bull_evidence: list[str] = field(default_factory=list)
    bear_evidence: list[str] = field(default_factory=list)
    bull_pct: int = 50
    bear_pct: int = 50


def evidence_bias(long_conds: list[str], short_conds: list[str]) -> tuple[int, int]:
    """多空證據傾向百分比(確定性加權:STRUCT 條件 ×2,其餘 ×1)。

    重要:這是「證據完整度的相對傾向」,不是勝率、不是漲跌機率(spec 二十一
    禁止輸出無統計依據的勝率數字)。兩邊皆無證據時回傳 50/50。
    """
    def weight(conds: list[str]) -> int:
        return sum(2 if c.startswith("STRUCT") else 1 for c in conds)

    wb, ws = weight(long_conds), weight(short_conds)
    if wb + ws == 0:
        return 50, 50
    bull = round(100 * wb / (wb + ws))
    return bull, 100 - bull


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
            ok.append("STRUCT:15分K低點越墊越高,漲勢沒斷(更高低點 HL)")
        if not up and "LH" in labels:
            ok.append("STRUCT:15分K高點越壓越低,跌勢沒停(更低高點 LH)")
        for ev in m15.events[-4:]:
            if not ev.still_valid or ev.provisional:
                continue
            if up and ev.event_type in ("BOS_UP", "CHOCH_UP"):
                ok.append(f"STRUCT:15分K收盤站上前高 {ev.price:.2f},順勢突破")
            if not up and ev.event_type in ("BOS_DOWN", "CHOCH_DOWN"):
                ok.append(f"STRUCT:15分K收盤跌破前低 {ev.price:.2f},順勢跌破")
            if up and ev.event_type == "FAILED_BREAKDOWN":
                ok.append("STRUCT:假跌破,價格又漲回來(跌不下去)")
            if not up and ev.event_type == "FAILED_BREAKOUT":
                ok.append("STRUCT:假突破,價格又跌回來(漲不上去)")

    # 位置條件:靠近支撐做多 / 靠近壓力做空
    zone_kind = "SUP_ZONE" if up else "RES_ZONE"
    zone = nearest_zone(levels, price, zone_kind, "STRONG")
    if zone and zone.price_low - atr15 <= price <= zone.price_high + atr15:
        ok.append(f"LEVEL:價格剛好在{'支撐' if up else '壓力'}區附近(有{'撐' if up else '壓'})")

    # 動能條件(1H)
    hist = indicators_h1.get("macd_hist")
    if hist is not None:
        if up and hist > 0:
            ok.append("MOMO:1小時動能偏多(MACD 在零軸上)")
        if not up and hist < 0:
            ok.append("MOMO:1小時動能偏空(MACD 在零軸下)")

    # 高週期不否決
    if h4:
        if up and h4.trend in ("UP", "RANGE", "UNKNOWN"):
            ok.append("HTF:4小時大方向不擋你做多")
        if not up and h4.trend in ("DOWN", "RANGE", "UNKNOWN"):
            ok.append("HTF:4小時大方向不擋你做空")
    if h1:
        if up and h1.trend == "UP":
            ok.append("HTF:1小時趨勢向上")
        if not up and h1.trend == "DOWN":
            ok.append("HTF:1小時趨勢向下")
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
            flags.append(f"CHASE_{'LONG' if up else 'SHORT'}_RISK:離最近的"
                         f"{'支撐' if up else '壓力'}已經 "
                         f"{abs(price - ref) / max(atr15, 1e-9):.1f} 倍波動幅度,"
                         f"追{'高' if up else '低'}容易被套")

    # 距強壓力太近禁止追多;距強支撐太近禁止追空
    guard = nearest_zone(levels, price, "RES_ZONE" if up else "SUP_ZONE", "STRONG")
    if guard:
        edge = guard.price_low if up else guard.price_high
        dist = (edge - price) if up else (price - edge)
        if 0 <= dist < s.no_chase_near_level_atr_mult * atr15:
            flags.append(f"CHASE_{'LONG' if up else 'SHORT'}_RISK:"
                         f"{'上方' if up else '下方'} "
                         f"{dist / max(atr15, 1e-9):.2f} 倍波動就是強"
                         f"{'壓力' if up else '支撐'},這裡追{'多' if up else '空'}很容易被巴")
    return flags


def _latest_structure_event_id(direction: str,
                               structures: dict[str, StructureReport]) -> str | None:
    """本 setup 對應的最新 15M 結構事件識別碼(BUGFIX R3:可追溯)。

    只認「最新一筆」有效事件:支持本方向 → 掛其 ID;
    與本方向相反(如 CHoCH/BOS 向下 vs 多單)→ 回傳 None ——
    反轉後原方向 setup 不得再引用反轉前的舊結構事件。
    """
    m15 = structures.get("15M")
    if not m15:
        return None
    up = direction == "LONG"
    wanted = ("BOS_UP", "CHOCH_UP", "FAILED_BREAKDOWN") if up else \
             ("BOS_DOWN", "CHOCH_DOWN", "FAILED_BREAKOUT")
    for ev in reversed(m15.events):
        if not ev.still_valid or ev.provisional:
            continue
        return (f"15M:{ev.event_type}:{ev.time.isoformat()}"
                if ev.event_type in wanted else None)
    return None


def _build_scenario(direction: str, conditions: list[str], *, price: float,
                    levels: list[CandidateLevel], atr15: float,
                    structures: dict[str, StructureReport]) -> tuple[Scenario, list[float]]:
    """組劇本(欄位全為候選 ID)並由 Python 計算 R/R(spec 十六)。

    BUGFIX R1/R2:單一函數一次性輸出完整物件(Scenario 為 frozen,禁止逐欄修改);
    輸出前強制通過 Invariant 驗證,違反 → INVALID + 剝除價位,絕不顯示錯誤價位。
    """
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

    # ── P1 產生端不變式:產不出合法 SL 就不產出停損(不硬湊、不送出矛盾組合)──
    from app.engines.setup_validator import (
        has_fatal, log_invalid, stop_side_ok, validate_prices_detailed,
    )
    entry_px = entry.mid if entry else None
    stop_px = (stop_ref.mid - (0.25 * atr15 if up else -0.25 * atr15)) if stop_ref else None
    stop_dropped = False
    if entry_px is not None and stop_px is not None and \
            not stop_side_ok(direction, entry_px, stop_px):
        logger.info("SETUP_STOP_DROPPED direction=%s entry=%.2f structural_sl=%.2f "
                    "(結構停損點在進場區錯誤一側,拒絕產出停損)",
                    direction, entry_px, stop_px)
        stop_ref = None
        stop_px = None
        stop_dropped = True
        status = "WATCH"   # 無合法停損 → 不得為可執行方案
    tp_mids = [t.mid for t in targets]
    if stop_px is None:
        rr = []   # 無停損 → 風報比不可計算,不得顯示殘留數字

    # ── BUGFIX R2:Invariant 驗證(進 UI/決策評分之前;防禦縱深)──
    detailed = validate_prices_detailed(direction, entry=entry_px, sl=stop_px,
                                        tps=tp_mids, current_price=price)
    reasons = [r["msg"] for r in detailed]
    fatal = has_fatal(detailed)
    event_id = _latest_structure_event_id(direction, structures)

    if reasons:
        # 攔截器接到 FATAL = 上游產生端已出錯 → ERROR log 附完整 setup(P1)
        log_invalid(direction, {
            "entry": entry_px, "sl": stop_px, "tps": tp_mids, "rr": rr,
            "price": price, "structure_event_id": event_id,
        }, reasons, fatal=fatal)
        scenario = Scenario(
            status="INVALID",
            setup=("偵測到自相矛盾的價位組合,已攔截;"
                   "等待下一次結構更新後重新計算"),
            invalid_reasons=reasons,
            invalid_fatal=fatal,
            structure_event_id=event_id,
        )
        return scenario, []

    scenario = Scenario(
        status=status,
        setup=f"{'多方' if up else '空方'}:{'; '.join(conditions[:3]) if conditions else '條件未成立,等待'}",
        entry_zone_id=entry.level_id if entry else None,
        required_confirmations=(
            ([] if status == "PREPARE" else
             [f"等待 15M 已收線{'突破局部高點/形成 HL' if up else '跌破局部低點/形成 LH'}",
              "等待價格回到理想進場區(非追價位置)"])
            + (["結構停損點位於進場區錯誤一側,暫無法定位合法停損,等待結構更新"]
               if stop_dropped else [])),
        stop_loss_id=stop_ref.level_id if stop_ref else None,
        target_ids=[t.level_id for t in targets],
        risk_reward=rr,
        invalidation_id=stop_ref.level_id if stop_ref else None,
        structure_event_id=event_id,
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
        # 休市但資料完好:仍計算多空證據傾向(基於最後已收線結構),決策維持 NO_TRADE。
        # 資料品質不良時不計算 —— 壞資料算出的傾向比沒有更危險。
        bull_conds: list[str] = []
        bear_conds: list[str] = []
        if code == "NO_TRADE_MARKET_CLOSED" and quality.status == "GOOD" and structures:
            bull_conds = _direction_conditions("LONG", structures=structures,
                                               indicators_h1=indicators_h1, price=price,
                                               levels=levels, atr15=atr15)
            bear_conds = _direction_conditions("SHORT", structures=structures,
                                               indicators_h1=indicators_h1, price=price,
                                               levels=levels, atr15=atr15)
        bp, bs = evidence_bias(bull_conds, bear_conds)
        if code == "NO_TRADE_MARKET_CLOSED":
            reason = "現在休市,先不動作。"
        else:
            reason = "資料品質有狀況,先不進場,免得照到錯的價格做決定。"
        return RuleDecision("NO_TRADE", reason,
                            0, "X", empty_long, empty_short, no_trade_code=code,
                            bull_evidence=bull_conds, bear_evidence=bear_conds,
                            bull_pct=bp, bear_pct=bs)
    if event_lockout:
        return RuleDecision("NO_TRADE",
                            f"快公布重要數據了(剩 {s.event_lockout_minutes} 分鐘),"
                            f"先別進場,等公布後塵埃落定再看。",
                            0, "X", empty_long, empty_short, no_trade_code="EVENT_LOCKOUT")
    if market_state == "INSUFFICIENT_DATA":
        return RuleDecision("NO_TRADE", "資料還不夠,硬猜方向只會賠錢,先等一下。",
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

    # BUGFIX R2:主方向 setup 被攔截 → 決策卡「暫無有效方案」,證據分數不得沿用
    dominant_sc = long_sc if dominant == "LONG" else short_sc if dominant == "SHORT" else None
    if dominant_sc is not None and dominant_sc.status == "INVALID":
        bull_pct, bear_pct = evidence_bias(long_conds, short_conds)
        return RuleDecision(
            action="WATCH", reason="暫無有效方案:偵測到自相矛盾的價位組合,已攔截,等待下一次重算。",
            evidence_score=0, confidence_grade="X",
            long_scenario=long_sc, short_scenario=short_sc, chase_flags=chase,
            evidence=long_conds + short_conds,
            bull_evidence=long_conds, bear_evidence=short_conds,
            bull_pct=bull_pct, bear_pct=bear_pct)

    if dominant is None or max(n_long, n_short) < 2 or market_state in ("RANGE", "COMPRESSION"):
        action, grade = "WATCH", "C"
        reason = (f"現在是{state_zh(market_state)},做多做空的理由都不夠強"
                  f"(多方 {n_long} 個、空方 {n_short} 個),沒把握就先看著。")
    else:
        d_zh = dir_zh(dominant)
        sc = long_sc if dominant == "LONG" else short_sc
        chase_this = [f for f in chase if dominant in f]
        if sc.status == "PREPARE" and rr_ok and not chase_this:
            action = f"PREPARE_{dominant}"
            grade = "A" if score >= 60 else "B"
            reason = (f"{d_zh}的條件湊齊了(含關鍵的順勢突破),賺賠比最高 {max(rr)} 倍、划算;"
                      f"等最後一個進場訊號出現就可以動手。")
        elif sc.status == "PREPARE" and not rr_ok:
            action, grade = "WATCH", "C"
            worst = rr[0] if rr else 0
            reason = (f"{d_zh}方向對,但這裡進場賺賠比只有 {worst} 倍,"
                      f"賺的比賠的還少、不划算,等更好的位置再說。")
        elif chase_this:
            action, grade = "WATCH", "C"
            desc = chase_this[0].split(":", 1)[1] if ":" in chase_this[0] else chase_this[0]
            reason = f"看得出想{d_zh},但現在追進去風險大:{desc}。"
        else:
            action, grade = "WATCH", "B" if market_state.startswith("STRONG") else "C"
            reason = f"{d_zh}方向,但還差關鍵的收線確認,再等一根 K 棒。"

    bull_pct, bear_pct = evidence_bias(long_conds, short_conds)
    return RuleDecision(action=action, reason=reason, evidence_score=score,
                        confidence_grade=grade, long_scenario=long_sc,
                        short_scenario=short_sc, chase_flags=chase,
                        evidence=long_conds + short_conds,
                        bull_evidence=long_conds, bear_evidence=short_conds,
                        bull_pct=bull_pct, bear_pct=bear_pct)

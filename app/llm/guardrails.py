"""AI 輸出守門(程式硬驗證,AI 不可繞過)。

- ID 反查:AI 引用的價位 ID 必須存在於候選表(candidate levels + FVG),否則退回。
- 價位不變式:沿用 setup_validator(Buy 停損低於進場、TP 遞增、±5% 帶、rr 檢查)。
- 機率總和:win_rates 與 scenarios 合計 100(±10 內自動歸一,否則退回)。
- 禁語:Wait 而無 wait_condition / next_trigger → 退回(禁止純觀望)。
- 事件鎖定:gates.event_lockout=true 而 AI 給 Buy/Sell → 程式直接蓋章改 Wait(不退回)。
"""
from __future__ import annotations

import logging

from app.engines.setup_validator import has_fatal, validate_prices_detailed
from app.schemas.ai import (
    AiAction, AiConfidence, AiMarketStructure, AiScenario, AiStrategy, AiTradePlan,
    AiWinRates,
)

logger = logging.getLogger(__name__)


def _mid(entry: dict) -> float | None:
    lo, hi = entry.get("price_low"), entry.get("price_high")
    if lo is None or hi is None:
        return None
    return (lo + hi) / 2


def _normalize_pair(a: int, b: int, tolerance: int = 10) -> tuple[int, int] | None:
    total = a + b
    if total == 100:
        return a, b
    if abs(total - 100) <= tolerance and total > 0:
        na = round(a * 100 / total)
        return na, 100 - na
    return None


def validate_and_build(raw: dict, resolve_table: dict[str, dict], *,
                       current_price: float,
                       event_lockout: bool) -> tuple[AiStrategy | None, list[str]]:
    """驗證決策引擎輸出;通過 → (AiStrategy, []),失敗 → (None, 錯誤清單)。"""
    errors: list[str] = []

    action_raw = raw.get("action", {}) or {}
    action_type = action_raw.get("type", "Wait")

    # ── 禁語:Wait 必附條件與下一步觸發 ──
    next_trigger = (action_raw.get("next_trigger") or "").strip()
    wait_condition = (action_raw.get("wait_condition") or "").strip()
    if not next_trigger:
        errors.append("next_trigger 空白:任何情況都必須給下一個高勝率進場條件")
    if action_type == "Wait" and not wait_condition:
        errors.append("action=Wait 但 wait_condition 空白:禁止純觀望")

    # ── ID 反查 ──
    ids = {k: raw.get(k) for k in ("entry_id", "stop_loss_id", "tp1_id", "tp2_id", "tp3_id")}
    unknown = [v for v in ids.values() if v and v not in resolve_table]
    if unknown:
        errors.append(f"引用了不存在的價位 ID:{unknown}")

    # ── Buy/Sell 必須有可執行方案 + 價位不變式 ──
    if action_type in ("Buy", "Sell") and not unknown:
        if not ids["entry_id"] or not ids["stop_loss_id"]:
            errors.append(f"action={action_type} 但缺 entry_id 或 stop_loss_id")
        else:
            direction = "LONG" if action_type == "Buy" else "SHORT"
            entry = _mid(resolve_table[ids["entry_id"]])
            sl = _mid(resolve_table[ids["stop_loss_id"]])
            tps = [_mid(resolve_table[ids[k]]) for k in ("tp1_id", "tp2_id", "tp3_id")
                   if ids[k]]
            tps = [t for t in tps if t is not None]
            if entry is None or sl is None:
                errors.append("entry/stop_loss ID 無法反查出價位")
            else:
                detailed = validate_prices_detailed(direction, entry=entry, sl=sl,
                                                    tps=tps, current_price=current_price)
                if has_fatal(detailed):
                    errors.append("價位不變式 FATAL:" +
                                  ";".join(r["msg"] for r in detailed
                                           if r["severity"] == "FATAL"))

    # ── 勝率合計 100 ──
    wr = raw.get("win_rates", {}) or {}
    pair = _normalize_pair(int(wr.get("long_pct", 0)), int(wr.get("short_pct", 0)))
    if pair is None:
        errors.append(f"win_rates 合計須為 100(收到 {wr})")

    # ── 三情境合計 100 ──
    sc_raw = (raw.get("scenarios") or [])[:3]
    if len(sc_raw) != 3:
        errors.append(f"scenarios 須恰好 3 個(收到 {len(sc_raw)})")
        sc_norm = None
    else:
        total = sum(int(x.get("probability_pct", 0)) for x in sc_raw)
        if total == 100:
            sc_norm = [int(x.get("probability_pct", 0)) for x in sc_raw]
        elif abs(total - 100) <= 10 and total > 0:
            sc_norm = [round(int(x.get("probability_pct", 0)) * 100 / total)
                       for x in sc_raw]
            sc_norm[-1] += 100 - sum(sc_norm)
        else:
            errors.append(f"scenarios 機率合計須為 100(收到 {total})")
            sc_norm = None

    if errors:
        return None, errors

    # ── 事件鎖定:程式蓋章(不退回)──
    gate_note = ""
    if event_lockout and action_type in ("Buy", "Sell"):
        gate_note = (f"程式風控蓋章:重大數據前鎖定,AI 原建議 {action_type} 已強制改為 "
                     "Wait(此規則 AI 不可推翻)")
        action_type = "Wait"
        wait_condition = wait_condition or "等重大數據公布、且公布後第一根 15 分K 收盤"

    conf = raw.get("confidence", {}) or {}
    strategy = AiStrategy(
        available=True,
        gate_note=gate_note,
        market_structure=AiMarketStructure(**(raw.get("market_structure") or {})),
        win_rates=AiWinRates(long_pct=pair[0], short_pct=pair[1]),
        action=AiAction(type=action_type, wait_condition=wait_condition,
                        next_trigger=next_trigger),
        trade_plan=AiTradePlan(
            entry_id=ids["entry_id"], stop_loss_id=ids["stop_loss_id"],
            tp1_id=ids["tp1_id"], tp2_id=ids["tp2_id"], tp3_id=ids["tp3_id"],
            resolved={v: resolve_table[v] for v in ids.values()
                      if v and v in resolve_table}),
        invalidation=raw.get("invalidation", ""),
        rationale=(raw.get("rationale") or "")[:100],
        risk_warning=raw.get("risk_warning", ""),
        one_liner=raw.get("one_liner", ""),
        scenarios=[AiScenario(name=x.get("name", ""), probability_pct=p,
                              trigger=x.get("trigger", ""), plan=x.get("plan", ""))
                   for x, p in zip(sc_raw, sc_norm)],
        confidence=AiConfidence(score=max(0, min(100, int(conf.get("score", 0)))),
                                factors=list(conf.get("factors") or [])[:4]),
    )
    return strategy, []

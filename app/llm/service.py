"""V2 AI 分析協調器:可用性 → 預算 → 指紋快取 → 3 分析師 → 決策 → 守門 → 落庫。

失敗策略:AI 層任何失敗都不影響確定性引擎輸出 —— ai_strategy.available=False,
其餘欄位照常。守門連續退回 → invalid=True(NO_TRADE_AI_INVALID)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import AiAnalysis
from app.db.session import db_session
from app.llm.agents import run_analysts, run_decision
from app.llm.client import llm_available
from app.llm.guardrails import validate_and_build
from app.llm.snapshot import build_snapshot, fingerprint_of
from app.llm.usage import budget_exceeded
from app.schemas.ai import AiStrategy

logger = logging.getLogger(__name__)


def _cached(fingerprint: str) -> dict | None:
    s = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=s.llm_cache_minutes)
    try:
        with db_session() as db:
            row = db.execute(select(AiAnalysis)
                             .where(AiAnalysis.fingerprint == fingerprint,
                                    AiAnalysis.created_at >= cutoff)
                             .order_by(AiAnalysis.created_at.desc())).scalars().first()
            return dict(row.payload) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai cache lookup failed: %s", exc)
        return None


def _persist(fingerprint: str, strategy: AiStrategy, run_id: int | None) -> None:
    try:
        with db_session() as db:
            db.add(AiAnalysis(analysis_run_id=run_id, fingerprint=fingerprint,
                              payload=strategy.model_dump(), model=strategy.model,
                              cost_usd=strategy.cost_usd,
                              created_at=datetime.now(timezone.utc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai persist failed: %s", exc)


async def generate_ai_strategy(*, price: float, atr15: float, state: str,
                               quality_status: str, ev, ind: dict, structures: dict,
                               levels: list, dfs_closed: dict, bias,
                               position: dict | None, no_signal: bool,
                               run_id: int | None = None) -> AiStrategy:
    s = get_settings()
    now_iso = datetime.now(timezone.utc).isoformat()

    ok, reason = llm_available()
    if not ok:
        return AiStrategy(unavailable_reason=reason, generated_at=now_iso)

    # ── 程式硬性閘門(AI 不可推翻;省 token:直接不呼叫)──
    if no_signal:
        return AiStrategy(unavailable_reason="Offset 未校準(NO-SIGNAL),暫停 AI 出訊",
                          generated_at=now_iso,
                          gate_note="程式風控:價格校正未完成前不產生任何可執行價位")
    if quality_status == "FAILED":
        return AiStrategy(unavailable_reason="資料品質 FAILED,AI 分析暫停",
                          generated_at=now_iso)

    over, spent = budget_exceeded()
    if over:
        return AiStrategy(unavailable_reason=(
            f"已達每日 AI 預算(今日 ${spent:.2f} / 上限 "
            f"${s.llm_daily_budget_usd:.2f}),今日改用純規則引擎"), generated_at=now_iso)

    # ── 程式端補算 FVG + 組快照 ──
    from app.engines.fvg import detect_fvg_multi
    atr_by_tf = {tf: ind.get(tf, {}).get("atr14") for tf in ("15M", "1H", "4H")}
    fvgs = detect_fvg_multi(dfs_closed, atr_by_tf=atr_by_tf)

    from app.services.cross_market import get_cross_market
    cross = await get_cross_market()

    snapshot = build_snapshot(
        price=price, atr15=atr15, state=state, quality_status=quality_status,
        ev=ev, ind=ind, structures=structures, levels=levels, fvgs=fvgs,
        bias=bias, position=position, cross=cross.to_prompt_dict(),
        no_signal=no_signal, event_lockout=ev.event_lockout)
    fp = fingerprint_of(snapshot)

    cached = _cached(fp)
    if cached is not None:
        try:
            st = AiStrategy(**cached)
            st.cache_hit = True
            st.fingerprint = fp
            return st
        except Exception:  # noqa: BLE001 — 舊格式不相容就重算
            pass

    resolve_table = {lv.level_id: lv.to_dict() for lv in levels}
    resolve_table.update({z.fvg_id: z.to_dict() for z in fvgs})

    try:
        analysts, cost = await run_analysts(snapshot)
        feedback: str | None = None
        strategy: AiStrategy | None = None
        errors: list[str] = []
        for attempt in range(1 + s.llm_max_retries):
            raw, c = await run_decision(snapshot, analysts, feedback=feedback)
            cost += c
            strategy, errors = validate_and_build(
                raw, resolve_table, current_price=price,
                event_lockout=ev.event_lockout)
            if strategy is not None:
                break
            feedback = ";".join(errors)
            logger.warning("AI decision rejected (attempt %d): %s", attempt + 1, feedback)

        if strategy is None:
            logger.error("NO_TRADE_AI_INVALID after retries: %s", errors)
            return AiStrategy(invalid=True, generated_at=now_iso, fingerprint=fp,
                              cost_usd=round(cost, 4), analysts=analysts,
                              unavailable_reason=("NO_TRADE_AI_INVALID:AI 輸出連續未通過"
                                                  f"程式驗證({';'.join(errors[:3])})"))

        strategy.analysts = analysts
        strategy.model = s.llm_model_decision
        strategy.cost_usd = round(cost, 4)
        strategy.fingerprint = fp
        strategy.generated_at = now_iso
        _persist(fp, strategy, run_id)
        return strategy
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI strategy generation failed: %s", exc)
        return AiStrategy(unavailable_reason=f"AI 呼叫失敗:{exc}", generated_at=now_iso)

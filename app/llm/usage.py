"""LLM 用量記帳與每日預算斷路器(llm_usage 表,UTC 日彙總)。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import LlmUsage
from app.db.session import db_session

logger = logging.getLogger(__name__)

# 每百萬 token 價格(input, output);未知模型以 Opus 級估計(寧可高估防爆預算)
PRICING_PER_M: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICE = (5.0, 25.0)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICING_PER_M.get(model, _DEFAULT_PRICE)
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)


def record_usage(model: str, input_tokens: int, output_tokens: int,
                 provider: str = "anthropic") -> float:
    """記入 llm_usage(當日彙總列);回傳本次成本。"""
    cost = estimate_cost(model, input_tokens, output_tokens)
    day = datetime.now(timezone.utc).date()
    try:
        with db_session() as db:
            row = db.execute(select(LlmUsage).where(
                LlmUsage.usage_day == day, LlmUsage.provider == provider,
                LlmUsage.model == model)).scalar_one_or_none()
            if row is None:
                row = LlmUsage(usage_day=day, provider=provider, model=model,
                               input_tokens=0, output_tokens=0, cost_usd=0.0, calls=0)
                db.add(row)
            row.input_tokens += input_tokens
            row.output_tokens += output_tokens
            row.cost_usd = round(row.cost_usd + cost, 6)
            row.calls += 1
    except Exception as exc:  # noqa: BLE001 — 記帳失敗不影響主流程
        logger.error("record_usage failed: %s", exc)
    return cost


def spent_today() -> float:
    """今日(UTC)所有模型合計花費。"""
    day = datetime.now(timezone.utc).date()
    try:
        with db_session() as db:
            rows = db.execute(select(LlmUsage.cost_usd)
                              .where(LlmUsage.usage_day == day)).scalars().all()
        return round(sum(rows), 6)
    except Exception as exc:  # noqa: BLE001
        logger.error("spent_today failed: %s", exc)
        return 0.0


def budget_exceeded() -> tuple[bool, float]:
    """(是否已達每日預算, 今日已花費)。達預算 → 當日不再呼叫 AI。"""
    from app.config import get_settings
    spent = spent_today()
    return spent >= get_settings().llm_daily_budget_usd, spent

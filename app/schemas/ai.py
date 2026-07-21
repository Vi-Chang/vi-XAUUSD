"""V2 AI 分析層輸出 Schema(固定 10 區塊,禁止自由發揮)。

紀律:
- 所有價位欄位一律為候選價位/FVG 的 ID,後端反查數字(防幻覺,沿用 spec 八)。
- 勝率欄位為「主觀評估機率」,非歷史統計勝率(原規格書二十一之誠實標示)。
- 禁止「觀望/再等等/沒有訊號/資訊不足」單獨出現:Wait 必附觸發條件。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AnalystView(BaseModel):
    """單一分析師(Macro / Technical / Sentiment)輸出。"""
    bias: Literal["BULLISH", "BEARISH", "NEUTRAL"] = "NEUTRAL"
    strength: int = 50                       # 0-100,對自身面向的傾向強度
    key_points: list[str] = Field(default_factory=list)
    one_line: str = ""


class AiMarketStructure(BaseModel):
    label: Literal["Bullish", "Bearish", "Range"] = "Range"
    reason: str = ""


class AiWinRates(BaseModel):
    long_pct: int = 50
    short_pct: int = 50
    disclaimer: str = "主觀評估機率(依當前證據),非歷史統計勝率"


class AiAction(BaseModel):
    type: Literal["Buy", "Sell", "Wait"] = "Wait"
    wait_condition: str = ""                 # Wait 時必填:等什麼
    next_trigger: str = ""                   # 下一個高勝率進場條件(永遠必填)


class AiTradePlan(BaseModel):
    """進場/停損/停利 — 全部用 ID,resolved 由後端填。"""
    entry_id: str | None = None
    stop_loss_id: str | None = None
    tp1_id: str | None = None
    tp2_id: str | None = None
    tp3_id: str | None = None
    resolved: dict = Field(default_factory=dict)   # 後端反查 {id: {price_low, price_high, ...}}


class AiScenario(BaseModel):
    name: str = ""
    probability_pct: int = 0
    trigger: str = ""
    plan: str = ""


class AiConfidence(BaseModel):
    score: int = 0                            # 0-100
    factors: list[str] = Field(default_factory=list)


class AiStrategy(BaseModel):
    """固定 10 區塊輸出 + 情境 + 信心 + 中繼資料。"""
    available: bool = False                   # False = 本次未產生(停用/預算/守門失敗)
    unavailable_reason: str = ""
    invalid: bool = False                     # 守門重試仍失敗 → NO_TRADE_AI_INVALID
    gate_note: str = ""                       # 程式硬性風控蓋章說明(AI 不可推翻)

    market_structure: AiMarketStructure = AiMarketStructure()      # 1
    win_rates: AiWinRates = AiWinRates()                           # 2
    action: AiAction = AiAction()                                  # 3
    trade_plan: AiTradePlan = AiTradePlan()                        # 4-6(進場/停損/TP1-3)
    invalidation: str = ""                                         # 7 失效條件
    rationale: str = ""                                            # 8 交易理由(≤100字)
    risk_warning: str = ""                                         # 9 風險提醒
    one_liner: str = ""                                            # 10 今日一句話結論

    scenarios: list[AiScenario] = Field(default_factory=list)      # 3 情境,合計 100%
    confidence: AiConfidence = AiConfidence()

    analysts: dict[str, AnalystView] = Field(default_factory=dict) # macro/technical/sentiment
    model: str = ""
    cost_usd: float = 0.0
    cache_hit: bool = False
    fingerprint: str = ""
    generated_at: str = ""


# ── 給 LLM 的結構化輸出 JSON Schema(strict:additionalProperties false)──

ANALYST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "bias": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL"]},
        "strength": {"type": "integer"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "one_line": {"type": "string"},
    },
    "required": ["bias", "strength", "key_points", "one_line"],
    "additionalProperties": False,
}

DECISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "market_structure": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "enum": ["Bullish", "Bearish", "Range"]},
                "reason": {"type": "string"},
            },
            "required": ["label", "reason"], "additionalProperties": False,
        },
        "win_rates": {
            "type": "object",
            "properties": {"long_pct": {"type": "integer"}, "short_pct": {"type": "integer"}},
            "required": ["long_pct", "short_pct"], "additionalProperties": False,
        },
        "action": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["Buy", "Sell", "Wait"]},
                "wait_condition": {"type": "string"},
                "next_trigger": {"type": "string"},
            },
            "required": ["type", "wait_condition", "next_trigger"],
            "additionalProperties": False,
        },
        "entry_id": {"type": ["string", "null"]},
        "stop_loss_id": {"type": ["string", "null"]},
        "tp1_id": {"type": ["string", "null"]},
        "tp2_id": {"type": ["string", "null"]},
        "tp3_id": {"type": ["string", "null"]},
        "invalidation": {"type": "string"},
        "rationale": {"type": "string"},
        "risk_warning": {"type": "string"},
        "one_liner": {"type": "string"},
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "probability_pct": {"type": "integer"},
                    "trigger": {"type": "string"},
                    "plan": {"type": "string"},
                },
                "required": ["name", "probability_pct", "trigger", "plan"],
                "additionalProperties": False,
            },
        },
        "confidence": {
            "type": "object",
            "properties": {
                "score": {"type": "integer"},
                "factors": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["score", "factors"], "additionalProperties": False,
        },
    },
    "required": ["market_structure", "win_rates", "action", "entry_id", "stop_loss_id",
                 "tp1_id", "tp2_id", "tp3_id", "invalidation", "rationale",
                 "risk_warning", "one_liner", "scenarios", "confidence"],
    "additionalProperties": False,
}

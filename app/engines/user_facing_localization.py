"""Final Telegram vocabulary boundary; strategy enums remain unchanged."""
from __future__ import annotations

import re
from typing import Any

_TERMS = {
    "COUNTER_HIGHER_TIMEFRAME_RISK": "與高週期方向相反，本單採短打",
    "MISSED_REQUIRES_PRIOR_ACTIONABLE_ENTRY": "這個機會從未正式可進場，因此不算錯過",
    "PRICE_OUTSIDE_CANDIDATE_NEIGHBORHOOD": "價格尚未接近候選進場區",
    "CONFIRMED_OPPOSITE_DEFENSE_BREAK": "原方向防守已由收盤確認失守",
    "SYMMETRIC_RESCAN_AFTER_DEFENSE_BREAK": "原方向失效後已立即掃描反向短線機會",
    "WAIT_BREAKOUT_CONFIRMATION": "等待突破收盤確認",
    "WAIT_PULLBACK_CONFIRMATION": "等待回踩止跌確認",
    "WAIT_BREAKOUT_RETEST": "等待突破後回測確認",
    "WAIT_CONFIRMATION": "等待收盤確認",
    "STRUCTURE_INVALIDATED": "短線結構已失效",
    "DATA_ENTRY_GATED": "行情資料尚不足以確認進場",
    "DATA_CAPABILITY_BLOCKS_WATCH": "行情資料不足，暫停建立觀察機會",
    "CURRENT_DIRECTIONAL_SCENARIO_INVALIDATED": "原方向交易機會已失效",
    "ESTIMATED_RR_TOO_LOW": "目前預估風險報酬比不划算",
    "RR_TOO_LOW": "目前風險報酬比不划算",
    "PRICE_TOO_EXTENDED": "價格已離合理進場區太遠",
    "NO_REACTION_CONFIRMATION": "尚未出現明確價格反應",
    "NO_STRUCTURE_CONFIRMATION": "短線結構尚未確認",
    "ENTRY_READY": "可以進場",
    "WAIT": "等待",
    "HOLD": "續抱",
    "REDUCE": "減倉",
    "EXIT": "退出",
    "BUY": "買進",
    "SELL": "賣出",
    "CORE": "核心部位",
    "DATA_GAP": "已收K線資料缺口",
    "PREPARE_LONG": "準備做多",
    "PREPARE_SHORT": "準備做空",
    "WATCH_LONG": "留意做多機會",
    "WATCH_SHORT": "留意做空機會",
    "MISSED_ENTRY": "機會已錯過，不追價",
    "INVALIDATED": "條件失效",
    "DEGRADED_15M": "15分鐘行情資料暫時不完整",
    "DEGRADED": "資料暫時不完整",
    "BLOCKED": "暫停進場",
    "BREAKDOWN": "跌破",
    "BREAKOUT": "突破",
    "RECLAIM": "跌破後收回",
    "LONG": "做多",
    "SHORT": "做空",
    "SL": "停損",
    "TP1": "第一停利",
    "TP2": "第二停利",
    "TP3": "第三停利",
    "RR": "風險報酬比",
    "UNKNOWN": "尚待確認",
    "PARSE_ERROR": "資料格式異常",
}

_PHRASES = {
    "Long Scenario": "做多劇本",
    "Short Scenario": "做空劇本",
    "tactical structure": "短線結構",
    "candidate zone": "候選進場區",
    "Data Health": "行情資料狀態",
    "setup": "交易機會",
    "trigger": "觸發條件",
    "reclaim": "跌破後收回",
    "breakdown": "跌破",
    "breakout": "突破",
}

_RAW_CODE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_SAFE_CODES = {"XAUUSD", "ATR", "MACD", "RSI", "API", "UTC", "VWAP"}


def translate_user_facing_term(value: Any) -> str | None:
    """Translate one internal term; null-like values are omitted."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "undefined", "nan", "n/a"}:
        return None
    return _TERMS.get(text, text)


def localize_user_facing_text(text: str) -> str:
    """Translate known tokens and prevent raw reason codes reaching Telegram."""
    result = text
    for source, target in sorted(_PHRASES.items(), key=lambda item: len(item[0]), reverse=True):
        result = re.sub(re.escape(source), target, result, flags=re.IGNORECASE)
    for source, target in sorted(_TERMS.items(), key=lambda item: len(item[0]), reverse=True):
        result = re.sub(rf"\b{re.escape(source)}\b", target, result)

    def replace_unknown(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in _SAFE_CODES or re.fullmatch(r"\d+[MHD]", token):
            return token
        return "系統條件"

    return _RAW_CODE.sub(replace_unknown, result)


def assert_no_internal_user_facing_terms(text: str) -> None:
    remaining = {token for token in _RAW_CODE.findall(text)
                 if token not in _SAFE_CODES and not re.fullmatch(r"\d+[MHD]", token)}
    if remaining:
        raise ValueError(f"UNLOCALIZED_USER_FACING_TERMS:{sorted(remaining)}")

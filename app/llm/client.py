"""Anthropic 客戶端包裝(V2 AI 層唯一出口)。

- 結構化輸出(output_config.format json_schema)保證回傳可解析 JSON。
- 每次呼叫自動記帳(llm_usage)。
- 測試以 set_client_for_tests() 注入假客戶端,不打真 API。
"""
from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.llm.usage import record_usage

logger = logging.getLogger(__name__)

_client = None          # AsyncAnthropic 單例
_test_client = None     # 測試注入


def set_client_for_tests(fake) -> None:
    """測試注入假客戶端(需有 async messages.create(**kwargs))。傳 None 還原。"""
    global _test_client
    _test_client = fake


def get_client():
    global _client
    if _test_client is not None:
        return _test_client
    if _client is None:
        from anthropic import AsyncAnthropic
        s = get_settings()
        _client = AsyncAnthropic(api_key=s.anthropic_api_key,
                                 timeout=float(s.llm_timeout_seconds), max_retries=1)
    return _client


def llm_available() -> tuple[bool, str]:
    """AI 層是否可用;不可用時回傳白話原因。"""
    s = get_settings()
    if not s.llm_enabled:
        return False, "AI 分析已在設定中關閉(LLM_ENABLED=false)"
    if _test_client is None and not s.anthropic_api_key:
        return False, "尚未設定 ANTHROPIC_API_KEY,AI 分析未啟用"
    if _test_client is None and s.mock_data_mode:
        return False, "Mock 資料模式不呼叫 AI(避免測試/開發花費)"
    return True, ""


async def call_json(*, model: str, system: str, user_payload: dict,
                    schema: dict, max_tokens: int = 2000,
                    thinking: bool = False) -> tuple[dict, float]:
    """單次結構化呼叫:回傳 (解析後 dict, 本次成本 USD)。

    - user_payload 以緊湊 JSON 序列化(sort_keys 確保指紋/快取穩定)。
    - thinking=True 時啟用 adaptive thinking(僅決策引擎;分析師不用,省 token)。
    """
    client = get_client()
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": json.dumps(
            user_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    resp = await client.messages.create(**kwargs)

    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cost = record_usage(model, in_tok, out_tok)

    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    return json.loads(text), cost

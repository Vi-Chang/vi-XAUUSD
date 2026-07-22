"""統一 LLM 呼叫模組(V2 AI 層唯一出口;供應商可切換)。

- 模式 A `LLM_PROVIDER=gemini`(預設):Google Gemini generateContent REST,免費層零成本。
- 模式 B `LLM_PROVIDER=openai_compatible`:OpenAI / Groq / DeepSeek / OpenRouter 相容端點,
  只改環境變數即可切換,不動程式碼。
- 節流(RPM 滑動視窗)、429 指數退避(1s→2s→4s,最多 3 次)、每日次數保護、
  用量記帳全部實作在本抽象層之上,換供應商時邏輯不變。
- API Key 一律走後端環境變數,嚴禁寫死或進前端 bundle。
- 測試以 set_client_for_tests() 注入假客戶端,不打真 API。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque

import httpx

from app.config import get_settings
from app.llm.usage import record_usage

logger = logging.getLogger(__name__)

_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)
_MAX_THROTTLE_WAIT = 30.0          # 排隊超過此秒數 → 直接拒絕(不讓分析卡死)

_test_client = None                # 測試注入:需有 async generate(prompt, max_tokens)
_call_times: deque[float] = deque()  # RPM 滑動視窗


class LlmRateLimitError(RuntimeError):
    """頻率/額度暫時性上限(對前端顯示友善繁中訊息)。"""


class LlmQuotaError(RuntimeError):
    """每日免費額度用完(當日不再呼叫)。"""


def set_client_for_tests(fake) -> None:
    """測試注入假客戶端(需有 async generate(prompt, max_tokens) →
    (text, input_tokens, output_tokens))。傳 None 還原。"""
    global _test_client
    _test_client = fake


def llm_available() -> tuple[bool, str]:
    """AI 層是否可用;不可用時回傳白話原因。"""
    s = get_settings()
    if not s.llm_enabled:
        return False, "AI 分析已在設定中關閉(LLM_ENABLED=false)"
    if _test_client is not None:
        return True, ""
    if s.mock_data_mode:
        return False, "Mock 資料模式不呼叫 AI(避免測試/開發花費)"
    if s.llm_provider == "gemini":
        if not s.gemini_api_key:
            return False, "尚未設定 GEMINI_API_KEY,AI 分析未啟用(Google AI Studio 可免費申請)"
    elif s.llm_provider == "openai_compatible":
        if not s.llm_base_url or not s.llm_api_key:
            return False, "openai_compatible 模式需設定 LLM_BASE_URL 與 LLM_API_KEY"
    else:
        return False, f"未知的 LLM_PROVIDER:{s.llm_provider}(支援 gemini / openai_compatible)"
    return True, ""


# ── 節流:RPM 滑動視窗 ────────────────────────────────────────

async def _acquire_slot() -> None:
    s = get_settings()
    waited = 0.0
    while True:
        now = time.monotonic()
        while _call_times and now - _call_times[0] > 60:
            _call_times.popleft()
        if len(_call_times) < s.llm_rpm_limit:
            _call_times.append(now)
            return
        wait = 60 - (now - _call_times[0]) + 0.05
        if waited + wait > _MAX_THROTTLE_WAIT:
            raise LlmRateLimitError("AI 呼叫頻率達每分鐘上限,請稍後再試")
        logger.info("LLM throttle: waiting %.1fs (window %d/%d)",
                    wait, len(_call_times), s.llm_rpm_limit)
        waited += wait
        await asyncio.sleep(wait)


# ── 供應商實作(僅此兩函式知道端點格式)──────────────────────

async def _generate_gemini(prompt: str, max_tokens: int) -> tuple[str, int, int]:
    s = get_settings()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{s.llm_model}:generateContent?key={s.gemini_api_key}")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens,
                             "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=float(s.llm_timeout_seconds)) as client:
        r = await client.post(url, json=body)
    if r.status_code == 429:
        raise LlmRateLimitError("AI 分析額度已用完,請稍後再試")
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    um = data.get("usageMetadata", {})
    return text, int(um.get("promptTokenCount", 0)), int(um.get("candidatesTokenCount", 0))


async def _generate_openai_compatible(prompt: str, max_tokens: int) -> tuple[str, int, int]:
    s = get_settings()
    url = s.llm_base_url.rstrip("/") + "/chat/completions"
    body = {"model": s.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens}
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    async with httpx.AsyncClient(timeout=float(s.llm_timeout_seconds)) as client:
        r = await client.post(url, json=body, headers=headers)
    if r.status_code == 429:
        raise LlmRateLimitError("AI 分析額度已用完,請稍後再試")
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    u = data.get("usage", {})
    return text, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


async def _generate(prompt: str, max_tokens: int) -> tuple[str, int, int]:
    if _test_client is not None:
        return await _test_client.generate(prompt, max_tokens)
    s = get_settings()
    if s.llm_provider == "gemini":
        return await _generate_gemini(prompt, max_tokens)
    if s.llm_provider == "openai_compatible":
        return await _generate_openai_compatible(prompt, max_tokens)
    raise RuntimeError(f"未知的 LLM_PROVIDER:{s.llm_provider}")


# ── JSON 解析(容錯:剝 markdown 圍欄/擷取首尾大括號)────────

def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise json.JSONDecodeError("no JSON object found", text[:80], 0)


# ── 對外唯一介面 ─────────────────────────────────────────────

async def call_json(*, system: str, user_payload: dict, schema: dict,
                    max_tokens: int = 2000) -> tuple[dict, float]:
    """單次結構化呼叫:回傳 (解析後 dict, 本次成本 USD;免費層為 0)。

    system prompt 併入 prompt 開頭 + JSON Schema 指示 + 緊湊 JSON 輸入,
    確保換供應商時原有指示(防幻覺、候選 ID 規則)完整保留。
    """
    s = get_settings()

    # 每日免費額度保護(Gemini 免費層 250/日;預設上限 200 留餘裕)
    from app.llm.usage import calls_today
    if calls_today() >= s.llm_daily_call_limit:
        raise LlmQuotaError(
            f"今日 AI 分析額度已用完({s.llm_daily_call_limit} 次),"
            "明日自動恢復;期間改用純規則引擎")

    prompt = (
        f"{system}\n\n"
        "【輸出格式】只輸出一個符合以下 JSON Schema 的 JSON 物件,"
        "不要 markdown 圍欄、不要任何多餘文字:\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n\n"
        "【輸入資料】\n"
        f"{json.dumps(user_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}")

    await _acquire_slot()

    last_exc: Exception | None = None
    for attempt in range(1 + len(_BACKOFF_SECONDS)):
        try:
            text, in_tok, out_tok = await _generate(prompt, max_tokens)
            break
        except (LlmRateLimitError, httpx.HTTPError) as exc:
            last_exc = exc
            if attempt < len(_BACKOFF_SECONDS):
                delay = _BACKOFF_SECONDS[attempt]
                logger.warning("LLM call failed (attempt %d: %s), retrying in %.0fs",
                               attempt + 1, exc, delay)
                await asyncio.sleep(delay)
    else:
        if isinstance(last_exc, LlmRateLimitError):
            raise last_exc
        raise LlmRateLimitError("AI 服務暫時無法使用,請稍後再試") from last_exc

    cost = record_usage(s.llm_model, in_tok, out_tok, provider=s.llm_provider)
    return _parse_json(text), cost

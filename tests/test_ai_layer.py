"""V2 AI 分析層測試(全程注入假 LLM 客戶端,不打真 API)。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app.db.session import init_db
from app.engines.fvg import detect_fvg
from app.engines.key_levels import CandidateLevel
from app.llm.client import set_client_for_tests
from app.llm.guardrails import validate_and_build
from app.llm.snapshot import build_snapshot, fingerprint_of
from app.llm.usage import estimate_cost, record_usage, spent_today
from app.schemas.ai import ANALYST_SCHEMA
from app.schemas.analysis import BiasAnalysis


# ── 測試素材 ─────────────────────────────────────────────────

def _levels() -> list[CandidateLevel]:
    return [
        CandidateLevel("SUP_ZONE_01", "SUP_ZONE", 3980.0, 3982.0, "STRONG", ["t"]),
        CandidateLevel("RES_ZONE_01", "RES_ZONE", 4020.0, 4022.0, "STRONG", ["t"]),
        CandidateLevel("RES_ZONE_02", "RES_ZONE", 4040.0, 4042.0, "STRONG", ["t"]),
        CandidateLevel("SWING_LOW_15M_01", "SWING_LOW_15M", 3970.0, 3970.0, "INFO", ["t"]),
    ]


def _resolve_table():
    return {lv.level_id: lv.to_dict() for lv in _levels()}


def _ev(lockout=False):
    return SimpleNamespace(event_impact="LOW", time_risk="LOW", event_lockout=lockout,
                           next_event="", minutes_remaining=None)


def _good_decision() -> dict:
    return {
        "market_structure": {"label": "Bullish", "reason": "日線多頭排列"},
        "win_rates": {"long_pct": 60, "short_pct": 40},
        "action": {"type": "Buy", "wait_condition": "",
                   "next_trigger": "15分K收盤站上 RES_ZONE_01 上緣加碼"},
        "entry_id": "SUP_ZONE_01", "stop_loss_id": "SWING_LOW_15M_01",
        "tp1_id": "RES_ZONE_01", "tp2_id": "RES_ZONE_02", "tp3_id": None,
        "invalidation": "15分K收盤跌破 SWING_LOW_15M_01",
        "rationale": "回踩強支撐做多", "risk_warning": "留意美元反彈",
        "one_liner": "回踩買進,破低就跑",
        "scenarios": [
            {"name": "主劇本", "probability_pct": 50, "trigger": "守住支撐", "plan": "續抱"},
            {"name": "次劇本", "probability_pct": 30, "trigger": "跌破支撐", "plan": "停損"},
            {"name": "黑天鵝", "probability_pct": 20, "trigger": "突發事件", "plan": "全出"},
        ],
        "confidence": {"score": 72, "factors": ["結構一致", "巨集面偏多"]},
    }


class FakeClient:
    """供應商無關的假客戶端:依 prompt 內容分辨分析師/決策呼叫。

    介面 = client._generate 的注入點:async generate(prompt, max_tokens)
    → (text, input_tokens, output_tokens)。
    """

    def __init__(self, decision_payloads: list[dict] | None = None,
                 fail_first_n: int = 0, truncated_first_n: int = 0):
        self.calls = 0
        self.decision_calls = 0
        self._decisions = decision_payloads or [_good_decision()]
        self._fail_remaining = fail_first_n           # 模擬前 N 次回 429
        self._truncated_remaining = truncated_first_n  # 模擬前 N 次回截斷的壞 JSON

    async def generate(self, prompt: str, max_tokens: int):
        self.calls += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            from app.llm.client import LlmRateLimitError
            raise LlmRateLimitError("AI 分析額度已用完,請稍後再試")
        if self._truncated_remaining > 0:
            self._truncated_remaining -= 1
            return '{"bias": "BULLISH", "strength": 62, "key_po', 1000, 300
        if "首席決策官" in prompt:
            idx = min(self.decision_calls, len(self._decisions) - 1)
            data = self._decisions[idx]
            self.decision_calls += 1
        else:
            data = {"bias": "BULLISH", "strength": 62,
                    "key_points": ["美元走弱"], "one_line": "偏多"}
        return json.dumps(data, ensure_ascii=False), 1000, 300


@pytest.fixture(autouse=True)
def _setup():
    init_db()
    import app.llm.client as client_mod
    client_mod._call_times.clear()      # RPM 視窗跨測試歸零,避免測試互相節流
    yield
    set_client_for_tests(None)


# ── FVG 偵測 ────────────────────────────────────────────────

def _df(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame(rows, index=idx)


def test_fvg_bull_detected_and_fill_excluded():
    # 第三根 low(4010)> 第一根 high(4005)→ 看多缺口 [4005, 4010]
    rows = [
        {"open": 4000, "high": 4005, "low": 3998, "close": 4004},
        {"open": 4004, "high": 4012, "low": 4003, "close": 4011},
        {"open": 4011, "high": 4018, "low": 4010, "close": 4016},
        {"open": 4016, "high": 4020, "low": 4012, "close": 4018},
    ]
    zones = detect_fvg(_df(rows), "15M", atr=5.0, min_atr_mult=0.1)
    assert len(zones) == 1
    z = zones[0]
    assert z.direction == "BULL" and z.fvg_id == "FVG_BULL_15M_01"
    assert z.price_low == 4005 and z.price_high == 4010

    # 之後跌回 4004(穿越整個缺口)→ 已回補,不再列出
    rows.append({"open": 4018, "high": 4019, "low": 4004, "close": 4006})
    assert detect_fvg(_df(rows), "15M", atr=5.0, min_atr_mult=0.1) == []


# ── 守門驗證 ────────────────────────────────────────────────

def test_guardrails_pass_and_resolve():
    st, errs = validate_and_build(_good_decision(), _resolve_table(),
                                  current_price=4000.0, event_lockout=False)
    assert errs == [] and st is not None and st.available
    assert st.trade_plan.entry_id == "SUP_ZONE_01"
    assert "SUP_ZONE_01" in st.trade_plan.resolved
    assert st.win_rates.long_pct + st.win_rates.short_pct == 100


def test_guardrails_unknown_id_rejected():
    bad = _good_decision()
    bad["entry_id"] = "SUP_ZONE_99"
    st, errs = validate_and_build(bad, _resolve_table(),
                                  current_price=4000.0, event_lockout=False)
    assert st is None and any("不存在的價位 ID" in e for e in errs)


def test_guardrails_wrong_stop_side_rejected():
    bad = _good_decision()
    bad["stop_loss_id"] = "RES_ZONE_01"      # Buy 停損高於進場 → FATAL
    bad["tp1_id"] = "RES_ZONE_02"
    bad["tp2_id"] = None
    st, errs = validate_and_build(bad, _resolve_table(),
                                  current_price=4000.0, event_lockout=False)
    assert st is None and any("FATAL" in e for e in errs)


def test_guardrails_pure_wait_forbidden():
    bad = _good_decision()
    bad["action"] = {"type": "Wait", "wait_condition": "", "next_trigger": ""}
    st, errs = validate_and_build(bad, _resolve_table(),
                                  current_price=4000.0, event_lockout=False)
    assert st is None
    assert any("next_trigger" in e for e in errs)
    assert any("純觀望" in e for e in errs)


def test_guardrails_probability_normalization():
    d = _good_decision()
    d["win_rates"] = {"long_pct": 58, "short_pct": 46}       # 104 → 歸一
    d["scenarios"][0]["probability_pct"] = 55                # 105 → 歸一
    st, errs = validate_and_build(d, _resolve_table(),
                                  current_price=4000.0, event_lockout=False)
    assert errs == []
    assert st.win_rates.long_pct + st.win_rates.short_pct == 100
    assert sum(s.probability_pct for s in st.scenarios) == 100


def test_guardrails_event_lockout_forces_wait():
    st, errs = validate_and_build(_good_decision(), _resolve_table(),
                                  current_price=4000.0, event_lockout=True)
    assert errs == []
    assert st.action.type == "Wait" and "鎖定" in st.gate_note


# ── 快照與指紋 ──────────────────────────────────────────────

def _snapshot(price=4000.0):
    return build_snapshot(price=price, atr15=5.0, state="RANGE",
                          quality_status="GOOD", ev=_ev(), ind={}, structures={},
                          levels=_levels(), fvgs=[], bias=BiasAnalysis(),
                          position=None, cross={"dxy": 104.1}, no_signal=False,
                          event_lockout=False)


def test_fingerprint_stable_within_bucket():
    assert fingerprint_of(_snapshot(4000.0)) == fingerprint_of(_snapshot(4000.9))
    assert fingerprint_of(_snapshot(4000.0)) != fingerprint_of(_snapshot(4030.0))


# ── 用量記帳 ────────────────────────────────────────────────

def test_usage_recording():
    from app.llm.usage import calls_today
    assert estimate_cost("gemini-2.5-flash", 1_000_000, 100_000) == 0.0   # 免費層 $0
    assert estimate_cost("gemini-2.5-pro", 1_000_000, 0) == 1.25          # 付費模型計價
    before_calls = calls_today()
    cost = record_usage("gemini-2.5-flash", 100_000, 10_000, provider="gemini")
    assert cost == 0.0
    assert calls_today() == before_calls + 1          # 次數照計(每日額度保護依據)


def test_429_exponential_backoff_then_success(monkeypatch):
    """模擬前兩次 429 → 退避重試後成功(退避秒數歸零加速測試)。"""
    import app.llm.client as client_mod
    monkeypatch.setattr(client_mod, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    fake = FakeClient(fail_first_n=2)
    set_client_for_tests(fake)
    data, cost = asyncio.run(client_mod.call_json(
        system="測試", user_payload={"x": 1}, schema=ANALYST_SCHEMA, max_tokens=100))
    assert data["bias"] == "BULLISH"
    assert fake.calls == 3                            # 2 次失敗 + 1 次成功


def test_429_retries_exhausted_raises_friendly(monkeypatch):
    import app.llm.client as client_mod
    monkeypatch.setattr(client_mod, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    set_client_for_tests(FakeClient(fail_first_n=99))
    with pytest.raises(client_mod.LlmRateLimitError) as ei:
        asyncio.run(client_mod.call_json(
            system="測試", user_payload={}, schema=ANALYST_SCHEMA, max_tokens=100))
    assert "請稍後再試" in str(ei.value)


def test_rpm_throttle_rejects_when_window_full():
    """滑動視窗滿載且需等待過久 → 友善拒絕,不讓分析卡死。"""
    import time as _t

    import app.llm.client as client_mod
    from app.config import get_settings
    client_mod._call_times.clear()
    now = _t.monotonic()
    for _ in range(get_settings().llm_rpm_limit):
        client_mod._call_times.append(now)
    with pytest.raises(client_mod.LlmRateLimitError):
        asyncio.run(client_mod._acquire_slot())
    client_mod._call_times.clear()


def test_truncated_json_retried_then_success(monkeypatch):
    """截斷的壞 JSON(如 maxOutputTokens 切斷)→ 重生成一次即恢復。"""
    import app.llm.client as client_mod
    monkeypatch.setattr(client_mod, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    fake = FakeClient(truncated_first_n=1)
    set_client_for_tests(fake)
    data, _ = asyncio.run(client_mod.call_json(
        system="測試", user_payload={}, schema=ANALYST_SCHEMA, max_tokens=100))
    assert data["bias"] == "BULLISH"
    assert fake.calls == 2


def test_json_fence_stripping():
    from app.llm.client import _parse_json
    assert _parse_json('{"a": 1}') == {"a": 1}
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('前言\n{"a": 1}\n後記') == {"a": 1}


# ── 服務協調(假客戶端)────────────────────────────────────

def _run_service(**over):
    from app.llm.service import generate_ai_strategy
    kwargs = dict(price=4000.0, atr15=5.0, state="RANGE", quality_status="GOOD",
                  ev=_ev(), ind={"15M": {"atr14": 5.0}}, structures={},
                  levels=_levels(), dfs_closed={}, bias=BiasAnalysis(),
                  position=None, no_signal=False)
    kwargs.update(over)
    return asyncio.run(generate_ai_strategy(**kwargs))


def test_service_end_to_end_with_fake_client():
    fake = FakeClient()
    set_client_for_tests(fake)
    st = _run_service()
    assert st.available and not st.invalid
    assert st.action.type == "Buy"
    assert st.analysts["macro"].bias == "BULLISH"
    assert st.cost_usd == 0.0       # Gemini 免費層 → 零成本
    assert st.model.startswith("gemini:")
    assert fake.calls == 4          # 3 分析師 + 1 決策


def test_service_cache_hit_second_time():
    set_client_for_tests(FakeClient())
    st1 = _run_service(state="BULLISH_PULLBACK")   # 獨立狀態,避免與其他測試共用指紋
    assert st1.available and not st1.cache_hit
    fake2 = FakeClient()
    set_client_for_tests(fake2)
    st2 = _run_service(state="BULLISH_PULLBACK")
    assert st2.available and st2.cache_hit
    assert fake2.calls == 0         # 指紋相同 → 完全不呼叫

def test_service_retry_then_invalid():
    bad = _good_decision()
    bad["entry_id"] = "SUP_ZONE_99"          # 每次都引用不存在 ID
    set_client_for_tests(FakeClient(decision_payloads=[bad, bad]))
    st = _run_service(state="COMPRESSION")   # 換狀態避開上一題快取
    assert not st.available and st.invalid
    assert "NO_TRADE_AI_INVALID" in st.unavailable_reason


def test_service_no_signal_gate_skips_llm():
    fake = FakeClient()
    set_client_for_tests(fake)
    st = _run_service(no_signal=True)
    assert not st.available and "NO-SIGNAL" in st.unavailable_reason
    assert fake.calls == 0


def test_service_budget_cutoff():
    """付費模型(價格表有價)超過每日預算 → 費用斷路器生效。"""
    from app.config import get_settings
    record_usage("gemini-2.5-pro", 0,
                 int(get_settings().llm_daily_budget_usd / 10 * 1e6) + 100_000)
    fake = FakeClient()
    set_client_for_tests(fake)
    st = _run_service(state="STRONG_BULL_TREND")
    assert not st.available and "預算" in st.unavailable_reason
    assert fake.calls == 0


def test_service_daily_call_quota():
    """免費層每日次數用完 → 友善繁中訊息,不再打 API。"""
    from datetime import datetime, timezone

    from app.config import get_settings
    from app.db.models import LlmUsage
    from app.db.session import db_session
    # 先清掉付費模型列(避免上一題的預算斷路器先觸發),再灌滿當日次數
    day = datetime.now(timezone.utc).date()
    with db_session() as db:
        for row in db.query(LlmUsage).filter(LlmUsage.usage_day == day).all():
            row.cost_usd = 0.0
        db.add(LlmUsage(usage_day=day, provider="gemini", model="quota-filler",
                        input_tokens=0, output_tokens=0, cost_usd=0.0,
                        calls=get_settings().llm_daily_call_limit))
    fake = FakeClient()
    set_client_for_tests(fake)
    st = _run_service(state="STRONG_BEAR_TREND")
    assert not st.available
    assert "額度已用完" in st.unavailable_reason
    assert fake.calls == 0

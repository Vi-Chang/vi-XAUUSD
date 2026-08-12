"""交易資格閘門(stale-data no-trade gate)測試矩陣。

涵蓋:無報價 / 無效時間 / NaN·Inf / ask<bid / 點差過大 / FAILED / 休市 / STALE /
fallback 快取過期 / DEGRADED / K 棒不足 / 證據不足;以及端到端(壞報價 → NO_TRADE、
零付費 AI 請求、劇本剝除)與公開投影相容性。全程不打真 AI、不用正式 DB/Secret。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.session import init_db
from app.engines.data_quality import DataQualityReport
from app.engines.trade_gate import evaluate_trade_eligibility
from app.llm.client import set_client_for_tests
from app.providers.base import PriceTick

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _tick(bid: float = 4000.0, ask: float = 4000.4, *, age_s: float = 1.0,
          quote_time: datetime | None = None) -> PriceTick:
    qt = quote_time if quote_time is not None else NOW - timedelta(seconds=age_s)
    return PriceTick(symbol="XAUUSD", bid=bid, ask=ask, quote_time=qt, provider="test")


def _q(status: str = "GOOD", *, market_open: bool = True,
       source_mismatch: bool = False) -> DataQualityReport:
    return DataQualityReport(status=status, market_open=market_open,
                             source_mismatch=source_mismatch)


def _eval(**kw):
    kw.setdefault("now", NOW)
    kw.setdefault("atr15", 5.0)
    return evaluate_trade_eligibility(**kw)


# ── 允許案例:新鮮、良好、開市、點差正常 ─────────────────────
def test_eligible_when_good_fresh_open():
    e = _eval(tick=_tick(), quality=_q(), market_state="RANGE", evidence_score=50)
    assert e.eligible and e.code == "OK"
    assert e.source_status == "OK" and e.market_status == "OPEN"
    assert e.spread_status == "OK" and e.evidence_status == "OK"
    assert e.data_age_seconds is not None and e.data_age_seconds >= 0


# ── 12 項阻擋條件 ────────────────────────────────────────────
def test_no_quote():
    e = _eval(tick=None, quality=_q())
    assert not e.eligible and e.code == "NO_QUOTE" and e.source_status == "NO_QUOTE"


def test_invalid_quote_time_none():
    t = _tick()
    t.quote_time = None  # type: ignore[assignment]
    e = _eval(tick=t, quality=_q())
    assert not e.eligible and e.code == "INVALID_QUOTE_TIME"


def test_invalid_quote_time_future():
    e = _eval(tick=_tick(quote_time=NOW + timedelta(seconds=120)), quality=_q())
    assert not e.eligible and e.code == "INVALID_QUOTE_TIME"


@pytest.mark.parametrize("bid,ask", [(float("nan"), 4000.4), (4000.0, float("inf"))])
def test_non_finite_price(bid, ask):
    e = _eval(tick=_tick(bid=bid, ask=ask), quality=_q())
    assert not e.eligible and e.code == "INVALID_QUOTE" and e.spread_status == "INVALID"


def test_ask_below_bid():
    e = _eval(tick=_tick(bid=4000.0, ask=3990.0), quality=_q())
    assert not e.eligible and e.code == "INVALID_QUOTE"


def test_non_positive_price():
    e = _eval(tick=_tick(bid=0.0, ask=1.0), quality=_q())
    assert not e.eligible and e.code == "INVALID_QUOTE"


def test_spread_too_wide():
    s = get_settings()
    cap = max(s.gate_spread_max_abs, s.gate_spread_max_atr15_mult * 5.0)
    e = _eval(tick=_tick(bid=4000.0, ask=4000.0 + cap + 10), quality=_q(), atr15=5.0)
    assert not e.eligible and e.code == "SPREAD_TOO_WIDE" and e.spread_status == "TOO_WIDE"


def test_data_failed():
    e = _eval(tick=_tick(), quality=_q("FAILED"))
    assert not e.eligible and e.code == "DATA_FAILED" and e.source_status == "FAILED"


def test_market_closed():
    e = _eval(tick=_tick(age_s=3600), quality=_q(market_open=False), market_state="RANGE")
    assert not e.eligible and e.code == "MARKET_CLOSED" and e.market_status == "CLOSED"


def test_market_closed_historical_mode_allowed():
    e = _eval(tick=_tick(), quality=_q(market_open=False), market_state="RANGE",
              evidence_score=50, historical_mode=True)
    assert e.eligible and e.market_status == "CLOSED"


def test_stale_quote():
    e = _eval(tick=_tick(age_s=5000), quality=_q("STALE"))
    assert not e.eligible and e.code == "STALE_QUOTE" and e.source_status == "STALE"


def test_fallback_cache_stale():
    s = get_settings()
    e = _eval(tick=_tick(age_s=s.gate_fallback_quote_max_age_seconds + 100),
              quality=_q(), is_fallback=True)
    assert not e.eligible and e.code == "FALLBACK_CACHE_STALE"


def test_fallback_within_limit_ok():
    s = get_settings()
    e = _eval(tick=_tick(age_s=max(1, s.gate_fallback_quote_max_age_seconds - 100)),
              quality=_q(), market_state="RANGE", evidence_score=50, is_fallback=True)
    assert e.eligible and e.code == "OK"


def test_degraded_status():
    e = _eval(tick=_tick(), quality=_q("DEGRADED"))
    assert not e.eligible and e.code == "DATA_DEGRADED" and e.source_status == "DEGRADED"


def test_degraded_source_mismatch():
    e = _eval(tick=_tick(), quality=_q("GOOD", source_mismatch=True))
    assert not e.eligible and e.code == "DATA_DEGRADED"


def test_insufficient_history():
    e = _eval(tick=_tick(), quality=_q(), market_state="INSUFFICIENT_DATA")
    assert not e.eligible and e.code == "INSUFFICIENT_HISTORY"


def test_no_evidence_when_actionable():
    e = _eval(tick=_tick(), quality=_q(), market_state="STRONG_BULL_TREND", evidence_score=5)
    assert not e.eligible and e.code == "NO_EVIDENCE" and e.evidence_status == "INSUFFICIENT"


def test_evidence_none_not_blocked():
    e = _eval(tick=_tick(), quality=_q(), market_state="RANGE", evidence_score=None)
    assert e.eligible and e.evidence_status == "UNKNOWN"


def test_reason_leaks_no_internal_detail():
    for e in (_eval(tick=None, quality=_q()),
              _eval(tick=_tick(bid=float("nan")), quality=_q()),
              _eval(tick=_tick(), quality=_q("FAILED"))):
        low = e.reason.lower()
        for banned in ("token", "secret", "password", "traceback", "exception",
                       "sqlite", "postgres", "http://", "https://"):
            assert banned not in low


# ── 端到端:壞報價 → NO_TRADE + 零付費 AI 請求 + 劇本剝除 ──────
class _CountingClient:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, prompt: str, max_tokens: int):
        self.calls += 1
        return json.dumps({"bias": "BULLISH", "strength": 60,
                           "key_points": ["x"], "one_line": "y"}), 100, 50


@pytest.fixture()
def _db_and_fake():
    init_db()
    import app.llm.client as cm
    cm._call_times.clear()
    fake = _CountingClient()
    set_client_for_tests(fake)
    yield fake
    set_client_for_tests(None)


def test_e2e_invalid_quote_forces_no_trade_and_zero_ai(_db_and_fake):
    from app.providers.mock import MockProvider
    from app.services.analysis_service import run_analysis
    bad = PriceTick(symbol="XAUUSD", bid=4000.0, ask=3990.0,   # ask < bid
                    quote_time=datetime.now(timezone.utc), provider="test")
    result = asyncio.run(run_analysis(MockProvider(), trigger="test", tick=bad))

    assert result.trade_eligibility.eligible is False
    assert result.trade_eligibility.code == "INVALID_QUOTE"
    # 公開市場層決策一律 NO_TRADE(公開端不得出現可執行 BUY/SELL)。
    assert result.market_decision.action == "NO_TRADE"
    # 私有決策:無持倉 → NO_TRADE;有持倉 → MANAGE(持倉管理非新入場,保留但附資料提醒)。
    assert result.decision.action in ("NO_TRADE", "MANAGE")
    # 不得保留可被誤認為有效的新入場指令(劇本一律剝除價位)。
    assert result.long_scenario.status != "PREPARE"
    assert not result.long_scenario.entry_zone_id and not result.long_scenario.target_ids
    assert result.short_scenario.status != "PREPARE"
    # 資料品質閘門在 AI 呼叫前執行 → 零付費 AI 請求。
    assert _db_and_fake.calls == 0
    assert result.ai_strategy.available is False


def test_e2e_public_projection_of_blocked_result(_db_and_fake):
    from app.providers.mock import MockProvider
    from app.services.analysis_service import run_analysis
    from app.services.public_view import assert_no_private_keys, public_analysis
    bad = PriceTick(symbol="XAUUSD", bid=4000.0, ask=3990.0,
                    quote_time=datetime.now(timezone.utc), provider="test")
    result = asyncio.run(run_analysis(MockProvider(), trigger="test", tick=bad))
    pub = public_analysis(result.model_dump())

    assert pub.get("available") is True
    assert pub["decision"]["action"] == "NO_TRADE"      # 公開端以 NO_TRADE 表達阻擋
    assert "trade_eligibility" not in pub               # 維持既有公開 schema 相容(不在 allowlist)
    assert assert_no_private_keys(pub) == []            # 公開 payload 無私人欄位

"""三層更新頻率架構:觸及偵測、冷卻、異常波動、第 3 層觸發、TD 軟上限降級。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.session import init_db
from app.providers.base import PriceTick
from app.services.tiered import EventCooldown, QuoteCache, check_structure_events


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    init_db()
    monkeypatch.setenv("TIER2_TOUCH_PCT", "0.0005")
    monkeypatch.setenv("TIER2_LEVEL_COOLDOWN_MINUTES", "60")
    monkeypatch.setenv("TIER2_ANOMALY_RANGE_MULT", "2.5")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_analysis_with_levels():
    """建一筆 analysis_run + 候選價位(供第 2 層讀取)。"""
    from app.db.models import AnalysisRun, CandidateLevel
    from app.db.session import db_session
    now = datetime.now(timezone.utc)
    with db_session() as db:
        run = AnalysisRun(run_time=now, trigger="test", market_state="RANGE",
                          decision_action="WATCH", confidence_grade="C",
                          evidence_score=0, data_quality_status="GOOD",
                          result_json={}, prompt_version="t", strategy_version="t",
                          model_version="t")
        db.add(run)
        db.flush()
        rows = [
            CandidateLevel(analysis_run_id=run.id, level_id="SUP_ZONE_01",
                           kind="SUP_ZONE", price_low=3990.0, price_high=3992.0,
                           strength="STRONG", source="test", created_at=now),
            CandidateLevel(analysis_run_id=run.id, level_id="SWING_HIGH_15M_01",
                           kind="SWING_HIGH_15M", price_low=4015.0, price_high=4015.0,
                           strength="INFO", source="test", created_at=now),
            CandidateLevel(analysis_run_id=run.id, level_id="SWING_LOW_15M_01",
                           kind="SWING_LOW_15M", price_low=3985.0, price_high=3985.0,
                           strength="INFO", source="test", created_at=now),
        ]
        for r in rows:
            db.add(r)
        return run.id


class TestTouchAndCooldown:
    def test_touch_triggers_event(self):
        _seed_analysis_with_levels()
        cache, cd = QuoteCache(), EventCooldown()
        # 3991 在 SUP_ZONE_01 區間內 → 距離 0 → 觸及
        events = check_structure_events(3991.0, cache, cd)
        assert any(e.key == "touch:SUP_ZONE_01" for e in events)
        assert any("觸及" in e.reason_zh and "支撐區" in e.reason_zh for e in events)

    def test_cooldown_blocks_repeat(self):
        _seed_analysis_with_levels()
        cache, cd = QuoteCache(), EventCooldown()
        first = check_structure_events(3991.0, cache, cd)
        assert any(e.key == "touch:SUP_ZONE_01" for e in first)
        # 60 分鐘內同價位再觸及 → 不得再觸發
        second = check_structure_events(3991.5, cache, cd)
        assert not any(e.key == "touch:SUP_ZONE_01" for e in second)
        # 冷卻過期後可再觸發
        cd._fired["touch:SUP_ZONE_01"] -= timedelta(minutes=61)
        third = check_structure_events(3991.0, cache, cd)
        assert any(e.key == "touch:SUP_ZONE_01" for e in third)

    def test_near_but_not_touching(self):
        _seed_analysis_with_levels()
        cache, cd = QuoteCache(), EventCooldown()
        # 距 SUP_ZONE 上緣 0.2% > 0.05% → 不觸發
        events = check_structure_events(4000.0, cache, cd)
        assert not any(e.key.startswith("touch:") for e in events)

    def test_break_high_and_low(self):
        _seed_analysis_with_levels()
        cache, cd = QuoteCache(), EventCooldown()
        up = check_structure_events(4016.0, cache, cd)
        assert any(e.key == "break:high" for e in up)
        cd2 = EventCooldown()
        down = check_structure_events(3984.0, QuoteCache(), cd2)
        assert any(e.key == "break:low" for e in down)


class TestAnomaly:
    def test_anomaly_detected(self):
        _seed_analysis_with_levels()
        cache, cd = QuoteCache(), EventCooldown()
        # 手工餵 20 個已收桶(平均振幅 1.0)+ 目前桶振幅 5.0(> 2.5 倍)
        cache._closed_ranges.extend([1.0] * 20)
        cache._bucket_hi, cache._bucket_lo = 4005.0, 4000.0
        events = check_structure_events(4000.0, cache, cd)
        assert any(e.key == "anomaly" for e in events)
        assert any("波動異常" in e.reason_zh for e in events)

    def test_warmup_no_false_anomaly(self):
        _seed_analysis_with_levels()
        cache, cd = QuoteCache(), EventCooldown()
        cache._closed_ranges.extend([1.0] * 5)   # 樣本不足 10 → 不判定
        cache._bucket_hi, cache._bucket_lo = 4050.0, 4000.0
        events = check_structure_events(4000.0, cache, cd)
        assert not any(e.key == "anomaly" for e in events)


class TestLayer3Trigger:
    async def test_event_triggers_full_analysis(self, monkeypatch):
        """模擬價格觸及關鍵價位 → 正確觸發第 3 層,且冷卻期內不重複觸發。"""
        import app.services.scheduler as sch
        _seed_analysis_with_levels()
        monkeypatch.setattr(sch, "market_is_open", lambda *a, **k: True)

        calls: list[dict] = []

        async def fake_full(*, trigger, reason_zh):
            calls.append({"trigger": trigger, "reason": reason_zh})
        monkeypatch.setattr(sch, "run_full_analysis", fake_full)

        st = sch.state
        st.quote_cache = QuoteCache()
        st.event_cooldown = EventCooldown()
        st.last_full_analysis = datetime.now(timezone.utc)  # 保底未到期
        st.quote_cache.add(PriceTick("XAUUSD", 3990.8, 3991.2,
                                     datetime.now(timezone.utc), "test"))

        await sch.job_structure_l2()
        assert len(calls) == 1
        assert calls[0]["trigger"] == "event"
        assert "SUP_ZONE_01" in calls[0]["reason"]

        # 冷卻期內同價位再跑 → 不重複觸發(保底也未到期)
        st.quote_cache.add(PriceTick("XAUUSD", 3991.0, 3991.4,
                                     datetime.now(timezone.utc), "test"))
        await sch.job_structure_l2()
        assert len(calls) == 1

    async def test_timed_fallback(self, monkeypatch):
        import app.services.scheduler as sch
        monkeypatch.setattr(sch, "market_is_open", lambda *a, **k: True)
        calls = []

        async def fake_full(*, trigger, reason_zh):
            calls.append(trigger)
        monkeypatch.setattr(sch, "run_full_analysis", fake_full)

        st = sch.state
        st.quote_cache = QuoteCache()   # 無新鮮報價 → 無事件
        st.event_cooldown = EventCooldown()
        st.last_full_analysis = datetime.now(timezone.utc) - timedelta(minutes=61)
        await sch.job_structure_l2()
        assert calls == ["timed"]


class TestSoftLimitDegrade:
    def test_td_soft_limit_flag(self, monkeypatch):
        from app.services.scheduler import _td_soft_limited
        import app.providers.twelve_data as td

        class FakeQuota:
            used_today = 650
        monkeypatch.setattr(td, "get_shared_quota", lambda: FakeQuota())
        assert _td_soft_limited() is True
        FakeQuota.used_today = 100
        assert _td_soft_limited() is False

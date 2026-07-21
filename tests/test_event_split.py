"""P2:事件固有影響力(event_impact)與時間風險(time_risk)兩維度分離。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import event_service as es


def _events(minutes_from_now: int, impact: str = "HIGH"):
    t = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return ([{"name": "FOMC Rate Decision", "country": "US",
              "time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "impact": impact}], False)


class TestDimensionSplit:
    def test_fomc_8_days_out(self, monkeypatch):
        """現象重現:FOMC 195 小時後 → 固有=高影響、時間風險=低,兩者不混。"""
        monkeypatch.setattr(es, "load_manual_events", lambda: _events(195 * 60))
        st = es.evaluate_event_risk()
        assert st.event_impact == "HIGH"     # 固有屬性不因距離遠而降級
        assert st.time_risk == "LOW"
        assert st.event_lockout is False
        assert "高影響" in st.reason and "緩衝充足" in st.reason
        assert "30 分鐘" in st.reason        # 文案與實際鎖定門檻一致

    def test_lockout_by_combination(self, monkeypatch):
        """鎖定 = 固有 HIGH + 剩餘 <= 鎖定分鐘(組合觸發,無需特判改等級)。"""
        monkeypatch.setattr(es, "load_manual_events", lambda: _events(20))
        st = es.evaluate_event_risk()
        assert st.event_impact == "HIGH" and st.time_risk == "HIGH"
        assert st.event_lockout is True

    def test_medium_time_window(self, monkeypatch):
        monkeypatch.setattr(es, "load_manual_events", lambda: _events(120))
        st = es.evaluate_event_risk()
        assert st.event_impact == "HIGH" and st.time_risk == "MEDIUM"
        assert st.event_lockout is False

    def test_low_impact_event_never_locks(self, monkeypatch):
        """低影響事件即使迫在眉睫,也不觸發鎖定(影響力是獨立維度)。"""
        monkeypatch.setattr(es, "load_manual_events",
                            lambda: _events(10, impact="LOW"))
        st = es.evaluate_event_risk()
        assert st.event_impact == "LOW"
        assert st.time_risk == "HIGH"        # 時間上很近
        assert st.event_lockout is False     # 但固有影響低 → 不鎖定
        assert "不觸發鎖定" in st.reason

    def test_no_upcoming_events(self, monkeypatch):
        monkeypatch.setattr(es, "load_manual_events", lambda: ([], False))
        st = es.evaluate_event_risk()
        assert st.time_risk == "LOW" and st.event_impact == "UNKNOWN"

    def test_legacy_level_aliases_time_risk(self, monkeypatch):
        monkeypatch.setattr(es, "load_manual_events", lambda: _events(195 * 60))
        st = es.evaluate_event_risk()
        assert st.level == st.time_risk      # 相容別名

"""分級通知(NOTIFY_LEVEL 門檻)+ 靜默 heartbeat 監控。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.session import init_db
from app.notifications.base import NotificationChannel, NotificationManager


class FakeChannel(NotificationChannel):
    def __init__(self, name: str, is_push: bool) -> None:
        self.name = name
        self.is_push = is_push
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


@pytest.fixture
def set_notify_level(monkeypatch):
    def _set(level: str, mention: str = ""):
        monkeypatch.setenv("NOTIFY_LEVEL", level)
        monkeypatch.setenv("TELEGRAM_MENTION", mention)
        monkeypatch.setenv("NOTIFY_COOLDOWN_SECONDS", "0")
        get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


def mgr():
    log = FakeChannel("log", is_push=False)
    push = FakeChannel("telegram", is_push=True)
    return NotificationManager([log, push]), log, push


class TestSeverityGating:
    async def test_warn_threshold_info_not_pushed(self, set_notify_level):
        set_notify_level("WARN")
        m, log, push = mgr()
        await m.notify("INFO", "heartbeat", "一切正常")
        assert len(log.sent) == 1          # log 永遠寫
        assert len(push.sent) == 0         # INFO < WARN → 不推

    async def test_warn_threshold_trigger_pushed(self, set_notify_level):
        set_notify_level("WARN")
        m, log, push = mgr()
        await m.notify("TRIGGER", "sig", "做多觸發")   # TRIGGER→WARN
        assert len(push.sent) == 1

    async def test_error_pushed_with_marker_and_mention(self, set_notify_level):
        set_notify_level("WARN", mention="@vi")
        m, log, push = mgr()
        await m.notify("RISK", "down", "provider 掛了")   # RISK→ERROR
        assert len(push.sent) == 1
        assert push.sent[0].startswith("@vi 🔴 [ERROR]")

    async def test_severity_override(self, set_notify_level):
        set_notify_level("WARN")
        m, log, push = mgr()
        # RISK 類別但覆寫為 WARN(資料延遲情境)
        await m.notify("RISK", "data_lag", "延遲", severity="WARN")
        assert push.sent[0].startswith("🟠 [WARN]")

    async def test_force_push_ignores_threshold(self, set_notify_level):
        set_notify_level("ERROR")   # 高門檻
        m, log, push = mgr()
        await m.notify("INFO", "daily", "每日摘要", force_push=True)
        assert len(push.sent) == 1

    async def test_info_threshold_pushes_everything(self, set_notify_level):
        set_notify_level("INFO")
        m, log, push = mgr()
        await m.notify("INFO", "hb", "ok")
        assert len(push.sent) == 1


class _FakeState:
    def __init__(self, notifier, provider_name="twelve_data"):
        self.notifier = notifier
        self.last_job_run: dict = {}
        self.last_daily_date = datetime.now(timezone.utc).date()  # 抑制每日摘要
        class _P: name = provider_name
        self.provider = _P()


def _insert_15m_candle(minutes_ago: float):
    from app.db.models import Candle
    from app.db.session import db_session
    from app.services.candle_service import candle_close_time
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    t = t.replace(second=0, microsecond=0)
    with db_session() as db:
        db.query(Candle).filter(Candle.timeframe == "15M").delete()
        db.add(Candle(symbol="XAUUSD", timeframe="15M", open_time=t,
                      close_time=candle_close_time(t, "15M"), open=4000, high=4001,
                      low=3999, close=4000.5, volume=100, is_closed=True,
                      data_provider="test", received_at=datetime.now(timezone.utc)))


class TestSilentMonitor:
    @pytest.fixture(autouse=True)
    def _db(self, monkeypatch):
        init_db()
        monkeypatch.setenv("NOTIFY_LEVEL", "WARN")
        monkeypatch.setenv("NOTIFY_COOLDOWN_SECONDS", "0")
        monkeypatch.setenv("DATA_LAG_WARN_MINUTES", "60")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    async def test_normal_no_push(self, monkeypatch):
        import app.services.heartbeat as hb
        monkeypatch.setattr(hb, "market_is_open", lambda *a, **k: True)
        _insert_15m_candle(5)   # 5 分鐘前,正常
        _, log, push = mgr()
        st = _FakeState(NotificationManager([log, push]))
        st.last_job_run = {"poll_price": datetime.now(timezone.utc),
                           "m15_analysis": datetime.now(timezone.utc)}
        await hb.run_monitor(st)
        assert len(push.sent) == 0   # 一切正常 → 不推播

    async def test_data_lag_warns(self, monkeypatch):
        import app.services.heartbeat as hb
        monkeypatch.setattr(hb, "market_is_open", lambda *a, **k: True)
        _insert_15m_candle(120)  # 2 小時前 → 延遲
        _, log, push = mgr()
        st = _FakeState(NotificationManager([log, push]))
        st.last_job_run = {"poll_price": datetime.now(timezone.utc),
                           "m15_analysis": datetime.now(timezone.utc)}
        await hb.run_monitor(st)
        assert len(push.sent) == 1
        assert "資料延遲" in push.sent[0]
        assert "[WARN]" in push.sent[0]

    async def test_component_down_errors(self, monkeypatch):
        import app.services.heartbeat as hb
        monkeypatch.setattr(hb, "market_is_open", lambda *a, **k: True)
        _insert_15m_candle(5)
        _, log, push = mgr()
        st = _FakeState(NotificationManager([log, push]))
        st.last_job_run = {}  # 沒有任何 job 執行過 → 死亡
        await hb.run_monitor(st)
        assert len(push.sent) == 1
        assert "[ERROR]" in push.sent[0]
        assert "元件停止運作" in push.sent[0]

    async def test_daily_summary_fires_once(self, monkeypatch):
        import app.services.heartbeat as hb
        monkeypatch.setattr(hb, "market_is_open", lambda *a, **k: False)  # 休市:只測每日摘要
        monkeypatch.setenv("DAILY_SUMMARY_HOUR_UTC", "0")  # 任何時間都可發
        get_settings.cache_clear()
        _, log, push = mgr()
        st = _FakeState(NotificationManager([log, push]))
        st.last_daily_date = None
        await hb.run_monitor(st)
        assert sum("[DAILY]" in x for x in push.sent) == 1
        # 同日再跑不重複
        await hb.run_monitor(st)
        assert sum("[DAILY]" in x for x in push.sent) == 1

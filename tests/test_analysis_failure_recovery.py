from types import SimpleNamespace

from app.db.session import init_db
from app.services.analysis_failure_recovery import AnalysisFailureRecovery


class Result:
    def model_dump(self):
        return {
            "symbol": "XAUUSD",
            "timestamp_utc": "2026-08-24T16:00:00+00:00",
            "normalized_analysis": {
                "trendBias": "bullish",
                "marketRegime": "bullish",
                "marketDataStatus": "GOOD",
                "lastClosedCandleTimestamp": "2026-08-24T15:45:00+00:00",
                "lastClosedCandlePrice": 100.0,
                "confirmationLevels": [],
            },
            "final_decision_state": {
                "marketDirection": "BULLISH",
                "selectedScenarioId": "S1",
                "qualityScore": 70,
            },
        }


class Notifier:
    def __init__(self):
        self.calls = []

    async def notify(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True


async def no_sleep(_seconds):
    return None


async def test_transient_failure_retries_silently_and_recovers(monkeypatch):
    init_db()
    monkeypatch.setattr(
        "app.services.analysis_failure_recovery.get_settings",
        lambda: SimpleNamespace(
            analysis_retry_delays_seconds=(0,), analysis_error_cooldown_seconds=600),
    )
    attempts = 0
    notifier = Notifier()

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")
        return Result()

    result, degraded = await AnalysisFailureRecovery().execute(
        operation, notifier=notifier, sleep=no_sleep)
    assert result is not None and degraded is None
    assert attempts == 2
    assert notifier.calls == []


async def test_terminal_failures_notify_once_keep_lkg_and_recovery_notifies_once(monkeypatch):
    init_db()
    monkeypatch.setattr(
        "app.services.analysis_failure_recovery.get_settings",
        lambda: SimpleNamespace(
            analysis_retry_delays_seconds=(0, 0, 0, 0),
            analysis_error_cooldown_seconds=600),
    )
    manager = AnalysisFailureRecovery()
    notifier = Notifier()

    async def seed_healthy():
        return Result()

    await manager.execute(seed_healthy, notifier=notifier, sleep=no_sleep)

    async def fail():
        raise RuntimeError("provider down")

    result, degraded = await manager.execute(
        fail, notifier=notifier, current=Result().model_dump(), sleep=no_sleep)
    assert result is None
    assert degraded["final_decision_state"]["state"] == "DATA_STALE"
    assert degraded["final_decision_state"]["entrySignal"] == "PAUSED"
    assert degraded["analysis_failure_recovery"]["entrySignalsPaused"] is True
    assert degraded["final_decision_state"]["marketDirection"] == "BULLISH"
    assert len(notifier.calls) == 1

    await manager.execute(
        fail, notifier=notifier, current=degraded, sleep=no_sleep)
    assert len(notifier.calls) == 1

    async def healthy():
        return Result()

    await manager.execute(healthy, notifier=notifier, sleep=no_sleep)
    assert len(notifier.calls) == 2
    assert "分析服務已恢復" in notifier.calls[-1][0][2]

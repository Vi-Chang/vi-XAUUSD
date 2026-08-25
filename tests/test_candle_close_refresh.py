from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.freshness import annotate_freshness
from app.services.scheduler import expected_closed_15m


def test_expected_closed_15m_uses_candle_open_timestamp_after_grace():
    now = datetime(2026, 8, 13, 17, 1, 30, tzinfo=timezone.utc)
    assert expected_closed_15m(now, delay_seconds=90) == datetime(
        2026, 8, 13, 16, 45, tzinfo=timezone.utc)
    before_grace = datetime(2026, 8, 13, 17, 1, 29, tzinfo=timezone.utc)
    assert expected_closed_15m(before_grace, delay_seconds=90) == datetime(
        2026, 8, 13, 16, 30, tzinfo=timezone.utc)


def test_0007_taipei_still_uses_2345_as_latest_closed_15m():
    # 2026-08-25 00:07 Asia/Taipei == 2026-08-24 16:07 UTC.  The 00:00
    # candle is still forming, so the latest closed candle opened at 23:45.
    now = datetime(2026, 8, 24, 16, 7, tzinfo=timezone.utc)
    assert expected_closed_15m(now, delay_seconds=0) == datetime(
        2026, 8, 24, 15, 45, tzinfo=timezone.utc)


def test_scheduler_checks_candle_close_every_30_seconds():
    from app.services.scheduler import build_scheduler

    scheduler = build_scheduler()
    job = scheduler.get_job("candle_close_refresh")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 30


async def test_close_refresh_runs_once_and_marks_current(monkeypatch):
    from app.services import scheduler

    expected = datetime(2026, 8, 13, 16, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler.state, "latest_result", {
        "normalized_analysis": {"lastClosedCandleTimestamp": "2026-08-13T16:30:00+00:00"}})
    monkeypatch.setattr(scheduler.state, "candle_refresh_bucket", None)
    monkeypatch.setattr(scheduler.state, "candle_refresh_attempts", 0)
    calls = []

    monkeypatch.setattr(scheduler, "market_is_open", lambda: True)
    monkeypatch.setattr(scheduler, "expected_closed_15m", lambda: expected)
    monkeypatch.setattr(scheduler, "get_settings", lambda: SimpleNamespace(
        candle_close_refresh_max_attempts=2))

    async def fake_broadcast(payload):
        calls.append(payload["type"])

    async def fake_analysis(*, trigger, reason_zh):
        calls.append(trigger)
        scheduler.state.latest_result["normalized_analysis"][
            "lastClosedCandleTimestamp"] = expected.isoformat()

    monkeypatch.setattr(scheduler, "broadcast_all", fake_broadcast)
    monkeypatch.setattr(scheduler, "run_full_analysis", fake_analysis)
    await scheduler.job_candle_close_refresh()
    await scheduler.job_candle_close_refresh()
    assert calls == ["analysis_refreshing", "candle_close"]
    assert scheduler.state.candle_refresh_attempts == 0


async def test_close_refresh_syncs_fresh_data_into_stale_decision_once(monkeypatch):
    from app.services import scheduler

    expected = datetime(2026, 8, 25, 7, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler.state, "latest_result", {
        "normalized_analysis": {
            "lastClosedCandleTimestamp": expected.isoformat(),
            "marketDataStatus": "GOOD",
        },
        "final_decision_state": {
            "dataHealth": "STALE",
            "scenarioValidity": "BLOCKED_BY_DATA",
        },
    })
    monkeypatch.setattr(
        scheduler.state,
        "candle_refresh_bucket",
        expected - scheduler.timedelta(minutes=15),
    )
    monkeypatch.setattr(scheduler.state, "candle_refresh_attempts", 1)
    monkeypatch.setattr(scheduler, "market_is_open", lambda: True)
    monkeypatch.setattr(scheduler, "expected_closed_15m", lambda: expected)
    calls = []

    async def fake_analysis(*, trigger, reason_zh):
        calls.append((trigger, reason_zh))

    monkeypatch.setattr(scheduler, "run_full_analysis", fake_analysis)

    await scheduler.job_candle_close_refresh()
    await scheduler.job_candle_close_refresh()

    assert [call[0] for call in calls] == ["candle_close_recovery_sync"]
    assert scheduler.state.candle_refresh_bucket == expected
    assert scheduler.state.candle_refresh_attempts == 0


async def test_close_refresh_bootstrap_does_not_replay_same_recovery_sample(
    monkeypatch,
):
    from app.services import scheduler

    expected = datetime(2026, 8, 25, 7, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler.state, "latest_result", {
        "normalized_analysis": {
            "lastClosedCandleTimestamp": expected.isoformat(),
            "marketDataStatus": "GOOD",
        },
        "final_decision_state": {
            "dataHealth": "STALE",
            "scenarioValidity": "BLOCKED_BY_DATA",
        },
    })
    monkeypatch.setattr(scheduler.state, "candle_refresh_bucket", None)
    monkeypatch.setattr(scheduler.state, "candle_refresh_attempts", 0)
    monkeypatch.setattr(scheduler, "market_is_open", lambda: True)
    monkeypatch.setattr(scheduler, "expected_closed_15m", lambda: expected)
    calls = []

    async def fake_analysis(*, trigger, reason_zh):
        calls.append((trigger, reason_zh))

    monkeypatch.setattr(scheduler, "run_full_analysis", fake_analysis)
    await scheduler.job_candle_close_refresh()

    assert calls == []
    assert scheduler.state.candle_refresh_bucket == expected


async def test_close_refresh_does_not_replay_when_analysis_is_ahead(monkeypatch):
    from app.services import scheduler

    expected = datetime(2026, 8, 25, 7, 45, tzinfo=timezone.utc)
    ahead = expected + scheduler.timedelta(minutes=15)
    monkeypatch.setattr(scheduler.state, "latest_result", {
        "normalized_analysis": {
            "lastClosedCandleTimestamp": ahead.isoformat(),
            "marketDataStatus": "GOOD",
        },
        "final_decision_state": {
            "dataHealth": "STALE",
            "scenarioValidity": "BLOCKED_BY_DATA",
        },
    })
    monkeypatch.setattr(
        scheduler.state,
        "candle_refresh_bucket",
        expected - scheduler.timedelta(minutes=15),
    )
    monkeypatch.setattr(scheduler.state, "candle_refresh_attempts", 0)
    monkeypatch.setattr(scheduler, "market_is_open", lambda: True)
    monkeypatch.setattr(scheduler, "expected_closed_15m", lambda: expected)
    calls = []

    async def fake_analysis(*, trigger, reason_zh):
        calls.append((trigger, reason_zh))

    monkeypatch.setattr(scheduler, "run_full_analysis", fake_analysis)
    await scheduler.job_candle_close_refresh()

    assert calls == []
    assert scheduler.state.candle_refresh_bucket == expected


async def test_close_refresh_does_not_resync_healthy_decision(monkeypatch):
    from app.services import scheduler

    expected = datetime(2026, 8, 25, 7, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler.state, "latest_result", {
        "normalized_analysis": {
            "lastClosedCandleTimestamp": expected.isoformat(),
            "marketDataStatus": "GOOD",
        },
        "final_decision_state": {
            "dataHealth": "HEALTHY",
            "scenarioValidity": "VALID",
        },
    })
    monkeypatch.setattr(scheduler.state, "candle_refresh_bucket", None)
    monkeypatch.setattr(scheduler.state, "candle_refresh_attempts", 0)
    monkeypatch.setattr(scheduler, "market_is_open", lambda: True)
    monkeypatch.setattr(scheduler, "expected_closed_15m", lambda: expected)
    calls = []

    async def fake_analysis(*, trigger, reason_zh):
        calls.append((trigger, reason_zh))

    monkeypatch.setattr(scheduler, "run_full_analysis", fake_analysis)
    await scheduler.job_candle_close_refresh()

    assert calls == []


def test_freshness_blocks_entry_while_new_closed_candle_is_pending():
    now = datetime(2026, 8, 13, 17, 3, tzinfo=timezone.utc)
    payload = {
        "timestamp_utc": now.isoformat(),
        "decision": {"action": "LONG", "confidence_grade": "A", "evidence_score": 80,
                     "reason": "ready"},
        "normalized_analysis": {
            "lastClosedCandleTimestamp": "2026-08-13T16:30:00+00:00",
            "entryReadiness": "ready", "entryTiming": "favorable",
            "longEntryAllowed": True, "shortEntryAllowed": False,
            "riskOverride": "none",
            "tradingDecision": {"newEntryDecision": {
                "readiness": "ready", "longAllowed": True, "shortAllowed": False}},
        },
    }
    out = annotate_freshness(payload, now=now)
    assert out["freshness"]["candle_refresh_pending"] is True
    assert out["decision"]["action"] == "WATCH"
    assert out["normalized_analysis"]["entryReadiness"] == "no_trade"
    assert out["normalized_analysis"]["longEntryAllowed"] is False

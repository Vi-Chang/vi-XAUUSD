from datetime import datetime, timezone

from app.services.heartbeat import _critical_jobs, check_liveness


def test_watchdog_covers_candle_decisions_and_telegram_outbox():
    jobs = _critical_jobs()
    assert "candle_close_refresh" in jobs
    assert "telegram_outbox" in jobs
    assert jobs["full_analysis"] <= 20 * 60


def test_missing_outbox_worker_is_a_bug_after_startup_grace():
    now = datetime.now(timezone.utc)
    healthy_other_jobs = {
        name: now for name in _critical_jobs() if name != "telegram_outbox"
    }
    dead = check_liveness(healthy_other_jobs, require_extended=True)
    assert any("telegram_outbox" in item for item in dead)

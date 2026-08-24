from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import scheduler


def test_outcome_backfill_default_batch_is_memory_bounded():
    assert Settings().outcome_backfill_batch_size <= 500


@pytest.mark.asyncio
async def test_outcome_backfills_release_each_database_session(monkeypatch):
    from app.db import session as session_module
    from app.services import (
        decision_event_outcomes,
        decision_replay,
        outcome_tracker,
        phase2_validation,
    )

    sessions = []

    @contextmanager
    def fake_db_session():
        token = object()
        sessions.append(token)
        yield token

    calls = []

    def record(name):
        def run(db, **_kwargs):
            calls.append((name, db))
            return 0

        return run

    monkeypatch.setattr(session_module, "db_session", fake_db_session)
    monkeypatch.setattr(outcome_tracker, "backfill_outcomes", record("outcome"))
    monkeypatch.setattr(
        decision_event_outcomes,
        "backfill_decision_event_outcomes",
        record("decision_event"),
    )
    monkeypatch.setattr(
        decision_replay,
        "backfill_decision_replay_outcomes",
        record("decision_replay"),
    )
    monkeypatch.setattr(
        phase2_validation,
        "backfill_setup_outcomes",
        record("phase2"),
    )
    monkeypatch.setattr(
        phase2_validation,
        "persist_daily_validation_report",
        lambda db, **_kwargs: calls.append(("report", db)),
    )
    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: SimpleNamespace(
            outcome_backfill_lookback_days=30,
            outcome_backfill_batch_size=250,
        ),
    )
    collections = []
    monkeypatch.setattr(scheduler.gc, "collect", lambda: collections.append(True))

    await scheduler.job_outcome_backfill()

    assert len(sessions) == 4
    assert len({id(item) for item in sessions}) == 4
    assert [name for name, _db in calls] == [
        "outcome",
        "decision_event",
        "decision_replay",
        "phase2",
        "report",
    ]
    assert [db for name, db in calls if name != "report"] == sessions
    assert calls[-1][1] is sessions[-1]
    assert len(collections) == 4

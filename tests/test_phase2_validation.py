from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import STRATEGY_BASELINE_VERSION, STRATEGY_FROZEN
from app.db.models import Candle, DecisionJournal, HumanOverrideAudit, SetupOutcome
from app.db.session import db_session, init_db
from app.services.phase2_validation import (
    _calibration,
    backfill_setup_outcomes,
    persist_decision_journals,
    record_human_override,
    validation_report,
)


def decision_input(setup_id="phase2-setup-1"):
    candidate = {
        "scenario_id": setup_id, "setup_type": "SWEEP_RECLAIM",
        "direction": "LONG", "lifecycle_state": "ENTRY_READY",
        "entry_zone": [100.0, 100.5], "invalidation_price": 98.0,
        "targets": [103.0, 105.0, 107.0], "risk_reward": 1.5,
        "raw_score": 80, "distance_atr": .2,
        "level_sources": {"trigger": {"price": 100.5},
                          "invalidation": {"price": 98.0}},
    }
    data = {
        "version": 42, "timestamp_utc": "2026-08-24T01:00:00+00:00",
        "normalized_analysis": {"currentPrice": 100.2, "marketDataTimestamp":
            "2026-08-24T01:00:00+00:00", "lastClosedCandleTimestamp":
            "2026-08-24T00:45:00+00:00"},
        "timeframes": {"h4": {"trend": "BULLISH"},
                       "h1": {"structure": "HIGHER_HIGH"}},
        "current_price": {"spread": .2}, "event_risk": {"status": "NORMAL"},
    }
    decision = {
        "decisionId": "decision-" + setup_id, "selectedScenarioId": setup_id,
        "signalCandidates": [candidate], "finalAction": "ENTER_LONG",
        "canEnter": True, "qualityGrade": "A", "primaryReason": "ENTRY_READY",
        "secondaryReasons": [],
    }
    return data, decision


def test_strategy_is_frozen_and_journal_is_insert_only():
    init_db(); data, decision = decision_input()
    with db_session() as db:
        assert persist_decision_journals(
            db, symbol="XAUUSD-PHASE2-JOURNAL", data=data, decision=decision) == 1
    changed, changed_decision = decision_input()
    changed_decision["signalCandidates"][0]["invalidation_price"] = 97.0
    with db_session() as db:
        assert persist_decision_journals(
            db, symbol="XAUUSD-PHASE2-JOURNAL", data=changed,
            decision=changed_decision) == 0
        row = db.execute(select(DecisionJournal).where(
            DecisionJournal.setup_id == "phase2-setup-1")).scalar_one()
        assert row.snapshot["hardInvalidation"] == 98.0
        assert row.strategy_version == STRATEGY_BASELINE_VERSION
        assert STRATEGY_FROZEN is True


def test_shadow_fill_uses_spread_slippage_and_future_received_candles_only():
    init_db(); now = datetime.now(timezone.utc); data, decision = decision_input(
        "phase2-realistic-fill")
    data["timestamp_utc"] = (now - timedelta(hours=5)).isoformat()
    with db_session() as db:
        persist_decision_journals(
            db, symbol="XAUUSD-PHASE2-FILL", data=data, decision=decision)
        journal = db.execute(select(DecisionJournal).where(
            DecisionJournal.setup_id == "phase2-realistic-fill")).scalar_one()
        journal.created_at = now - timedelta(hours=5)
        for index, close in enumerate((100.3, 101.2, 103.2, 102.8)):
            stamp = journal.created_at + timedelta(minutes=15 * (index + 1))
            db.add(Candle(symbol="XAUUSD-PHASE2-FILL", timeframe="15M",
                open_time=stamp - timedelta(minutes=15), close_time=stamp,
                open=close - .2, high=close + .3, low=99.9, close=close,
                volume=100, spread=.2, is_closed=True,
                data_provider="phase2-test", received_at=stamp))
    with db_session() as db:
        assert backfill_setup_outcomes(db, now=now) == 1
        outcome = db.execute(select(SetupOutcome).join(
            DecisionJournal, DecisionJournal.journal_id == SetupOutcome.journal_id
        ).where(DecisionJournal.setup_id == "phase2-realistic-fill")).scalar_one()
        assert outcome.outcome["entryCaptured"] is True
        assert outcome.outcome["fillPrice"] > 100.25
        assert outcome.outcome["spread"] == .2
        assert outcome.outcome["TP1Hit"] is True


def synthetic_rows(count=20, *, confidence=85, win_every=2):
    rows = []
    now = datetime.now(timezone.utc)
    for index in range(count):
        journal = DecisionJournal(
            journal_id=f"synthetic-{confidence}-{index}", setup_id=f"s-{index}",
            decision_id=f"d-{index}", strategy_version=STRATEGY_BASELINE_VERSION,
            symbol="X", strategy_type="RETEST", direction="LONG", is_primary=True,
            snapshot={"confidence": confidence, "features": {"htfAligned": index % 2}},
            post_analysis={}, created_at=now + timedelta(minutes=index))
        success = index % win_every == 0
        outcome = SetupOutcome(
            journal_id=journal.journal_id, status="COMPLETE",
            outcome={"success": success, "realizedR": 1 if success else -1,
                     "directionCorrect": success, "entryCaptured": True,
                     "missedValidEntry": False, "potentialFalseStop": False,
                     "maeR": .3, "mfeR": 1.2}, evaluated_through=now,
            created_at=now, updated_at=now)
        rows.append((journal, outcome))
    return rows


def test_overconfident_bucket_is_exposed_not_rewritten():
    report = _calibration(synthetic_rows(20, confidence=85, win_every=2))
    assert report["buckets"][0]["status"] == "OVERCONFIDENT"
    assert report["ECE"] > .3
    assert report["historicalProbabilityAllowed"] is False


def test_empty_live_database_cannot_claim_phase2_passed():
    init_db()
    with db_session() as db:
        report = validation_report(db)
    assert report["phase2ValidationPassed"] is False
    assert report["status"] == "COLLECTING_OR_NOT_VALIDATED"
    assert report["answers"]["trueAccuracy"] is None
    assert report["answers"]["bestSetups"] == []


def test_human_override_is_kept_out_of_system_outcome():
    init_db()
    with db_session() as db:
        record_human_override(
            db, journal_id="phase2-setup-1", override_type="DELAY_STOP",
            payload={"reason": "manual"})
    with db_session() as db:
        audit = db.execute(select(HumanOverrideAudit).where(
            HumanOverrideAudit.journal_id == "phase2-setup-1")).scalar_one()
        assert audit.override_type == "DELAY_STOP"
        report = validation_report(db)
        assert report["humanOverride"]["sampleSize"] >= 1

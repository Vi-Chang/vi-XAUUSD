from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Candle, DecisionReplay
from app.db.session import db_session, init_db
from app.services.decision_replay import (
    backfill_decision_replay_outcomes,
    replay_performance,
)


def _replay(decision_id: str, symbol: str, created_at: datetime,
            action: str = "WAIT") -> DecisionReplay:
    return DecisionReplay(
        decision_id=decision_id, symbol=symbol, decision_version=1,
        final_action=action, scenario_type="BREAKOUT_RETEST", raw_score=75,
        payload={
            "currentPrice": 100.0, "atr15": 2.0,
            "selectedScenarioId": "setup-1",
            "candidates": [{"scenario_id": "setup-1", "direction": "LONG",
                            "invalidation_price": 98.0, "risk_reward": 2.0}],
            "finalDecision": {"primaryReason": "RR_TOO_LOW", "secondaryReasons": []},
            "regime": {"compositeRegime": "BULLISH_RESTORED"},
        }, outcome={}, created_at=created_at)


def _candle(symbol: str, stamp: datetime, high: float, low: float,
            close: float, received_at: datetime | None = None) -> Candle:
    return Candle(symbol=symbol, timeframe="15M",
        open_time=stamp - timedelta(minutes=15), close_time=stamp,
        open=100, high=high, low=low, close=close, volume=100,
        is_closed=True, data_provider="replay-test", received_at=received_at or stamp)


def test_wait_outcome_records_missed_move_and_performance():
    init_db()
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=2)
    symbol = "XAUUSD-REPLAY-WAIT"
    with db_session() as db:
        db.add(_replay("replay-wait-1", symbol, start))
        db.add(_candle(symbol, start + timedelta(minutes=15), 102.2, 99.5, 102.0))
        db.add(_candle(symbol, start + timedelta(minutes=30), 104.2, 101.0, 104.0))
    with db_session() as db:
        assert backfill_decision_replay_outcomes(db, now=now) == 1
        row = db.execute(select(DecisionReplay).where(
            DecisionReplay.decision_id == "replay-wait-1")).scalar_one()
        assert row.outcome["missed_opportunity"] is True
        assert row.outcome["missed_long_move"] is True
        assert row.outcome["blocked_good_trade"] is True
        assert row.outcome["overly_strict_filter"] is True
        report = replay_performance(db)
        assert report["missedOpportunity"]["missed_long_move"] >= 1


def test_enter_outcome_records_stop_and_ignores_predecision_data():
    init_db()
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=1)
    symbol = "XAUUSD-REPLAY-ENTRY"
    with db_session() as db:
        db.add(_replay("replay-entry-1", symbol, start, "ENTER_LONG"))
        db.add(_candle(symbol, start + timedelta(minutes=15), 110, 90, 109,
                       received_at=start - timedelta(seconds=1)))
        db.add(_candle(symbol, start + timedelta(minutes=30), 100.5, 97.5, 98.0))
    with db_session() as db:
        assert backfill_decision_replay_outcomes(db, now=now) == 1
        row = db.execute(select(DecisionReplay).where(
            DecisionReplay.decision_id == "replay-entry-1")).scalar_one()
        assert row.outcome["entry_then_stop"] is True
        assert row.outcome["mfe_r"] < 1
        evaluated = row.outcome["evaluatedThrough"]
        assert backfill_decision_replay_outcomes(db, now=now) == 0
        assert row.outcome["evaluatedThrough"] == evaluated

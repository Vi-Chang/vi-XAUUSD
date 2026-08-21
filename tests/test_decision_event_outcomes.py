from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Candle, DecisionEvent, DecisionEventOutcome
from app.db.session import db_session, init_db
from app.services.decision_event_outcomes import (
    backfill_decision_event_outcomes,
    decision_event_performance,
)


def test_ready_event_is_scored_forward_only_with_execution_costs():
    init_db()
    now = datetime.now(timezone.utc)
    event_time = now - timedelta(hours=2)
    event_id = "event-outcome-long-1"
    with db_session() as db:
        db.add(DecisionEvent(
            event_id=event_id, symbol="XAUUSD-EVENT-OUTCOME", previous_state="LONG_WATCH",
            current_state="LONG_READY", transition_reason="confirmed", market_state="bullish",
            final_decision="LONG_READY", current_price=100.0, entry_zone={"low": 99, "high": 100},
            stop_loss=98.0, targets=[103.0], candle_close_time=event_time.isoformat(),
            calculated_at=event_time.isoformat(), data_version=1,
            payload={"executionCosts": {"spread": 0.2, "slippage": 0.1, "fees": 0}},
            created_at=event_time))
        for index, close in enumerate((101.0, 103.2, 102.5, 102.8, 103.0)):
            stamp = event_time + timedelta(minutes=15 * (index + 1))
            db.add(Candle(symbol="XAUUSD-EVENT-OUTCOME", timeframe="15M",
                open_time=stamp - timedelta(minutes=15), close_time=stamp,
                open=close - .2, high=close + .2, low=close - .4, close=close,
                volume=100, is_closed=True, data_provider="test", received_at=stamp))
    with db_session() as db:
        assert backfill_decision_event_outcomes(db, now=now) == 1
        row = db.execute(select(DecisionEventOutcome).where(
            DecisionEventOutcome.event_id == event_id)).scalar_one()
        assert row.tp1_hit is True
        assert row.transaction_cost == 0.3
        assert row.max_favorable_r > 1
        assert row.horizons["1h"]["net_r"] > 0
        report = decision_event_performance(db)
        assert "by_setup" in report
        assert report["settled_1h"] >= 1


def test_candle_received_before_event_is_never_used():
    init_db()
    now = datetime.now(timezone.utc)
    event_time = now - timedelta(hours=1)
    with db_session() as db:
        db.add(DecisionEvent(event_id="event-no-lookahead", symbol="XAUUSD-NO-LOOK",
            previous_state="LONG_WATCH", current_state="LONG_READY",
            transition_reason="confirmed", market_state="bullish",
            final_decision="LONG_READY", current_price=100, entry_zone=None,
            stop_loss=98, targets=[103], candle_close_time=event_time.isoformat(),
            calculated_at=event_time.isoformat(), data_version=1, payload={}, created_at=event_time))
        db.add(Candle(symbol="XAUUSD-NO-LOOK", timeframe="15M",
            open_time=event_time, close_time=event_time + timedelta(minutes=15),
            open=100, high=110, low=99, close=109, volume=100, is_closed=True,
            data_provider="test", received_at=event_time - timedelta(seconds=1)))
    with db_session() as db:
        assert backfill_decision_event_outcomes(db, now=now) == 0

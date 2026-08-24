from datetime import datetime, timedelta, timezone

import pytest

from app.engines.trade_plan import build_trade_plan
from app.engines.trading_invariants import (
    validate_stop_update,
    validate_trade_prices,
)
from app.providers.base import PriceTick
from app.services.tiered import QuoteCache


def tick(at: datetime, bid: float = 4600.0, ask: float = 4600.3) -> PriceTick:
    return PriceTick("XAUUSD", bid, ask, at, "fault-injection")


def test_late_tick_cannot_overwrite_authoritative_quote():
    cache = QuoteCache()
    latest = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)
    assert cache.add(tick(latest)) is True
    assert cache.add(tick(latest - timedelta(seconds=5), 4500, 4500.3)) is False
    assert cache.last_tick.mid == 4600.15
    assert cache.out_of_order_tick_count == 1


def test_exact_duplicate_tick_is_idempotent():
    cache = QuoteCache()
    at = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)
    assert cache.add(tick(at)) is True
    assert cache.add(tick(at)) is False
    assert cache.duplicate_tick_count == 1


def test_old_first_tick_cannot_look_fresh_only_because_it_was_just_received():
    cache = QuoteCache()
    cache.add(tick(datetime.now(timezone.utc) - timedelta(minutes=10)))
    assert cache.fresh_tick(max_age_seconds=60) is None


def test_invalid_spread_is_rejected_before_cache_write():
    cache = QuoteCache()
    with pytest.raises(ValueError, match="ask 不得低於 bid"):
        cache.add(tick(datetime.now(timezone.utc), 4601, 4600))
    assert cache.last_tick is None


@pytest.mark.parametrize(("direction", "previous", "new"), [
    ("LONG", 4615.0, 4594.9),
    ("SHORT", 4615.0, 4642.0),
])
def test_fault_injection_stop_widening_is_rejected(direction, previous, new):
    with pytest.raises(ValueError, match="不得"):
        validate_stop_update(direction, previous_stop=previous, new_stop=new)


def test_trade_price_direction_invariants_fail_closed():
    with pytest.raises(ValueError, match="多單停損"):
        validate_trade_prices("LONG", entry=4623.74, stop=4640, targets=[4650])
    with pytest.raises(ValueError, match="空單止盈"):
        validate_trade_prices("SHORT", entry=4623.74, stop=4640, targets=[4650])


def test_trade_plan_rejects_wrong_side_stop():
    plan, error = build_trade_plan({
        "setup_id": "bad-stop", "direction": "LONG",
        "suggested_entry": 4623.74, "stop_loss": 4640,
    }, symbol="XAUUSD", created_at="2026-08-24T01:00:00Z")
    assert plan == {}
    assert error == "多單停損必須低於進場價"

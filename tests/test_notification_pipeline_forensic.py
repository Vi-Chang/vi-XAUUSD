from datetime import datetime, timezone

from app.engines.decision_presentation import format_decision_message
from app.services.heartbeat import _market_reopen_grace
from app.services.notification_policy import (
    canonical_dedupe_key,
    eligibility,
    is_expired,
)


def event(event_type: str, **values):
    return {
        "symbol": "XAUUSD", "setupId": "setup-1", "positionId": "position-1",
        "event_type": event_type, "eventVersion": 1,
        "generatedAtUtc": "2026-08-24T01:00:00+00:00", **values,
    }


def test_candle_finalized_is_auditable_but_not_push_eligible():
    assert eligibility(event("CANDLE_FINALIZED")) == {
        "eligible": False, "reasonCode": "SKIP_LOW_PRIORITY", "priority": "DEBUG"}


def test_recovery_and_safety_events_are_push_eligible():
    assert eligibility(event("DATA_RECOVERED"))["eligible"] is True
    assert eligibility(event("STOP_TRIGGERED"))["priority"] == "CRITICAL"


def test_canonical_dedupe_keeps_different_event_types_distinct():
    recovered = canonical_dedupe_key(event("DATA_RECOVERED"))
    stale = canonical_dedupe_key(event("DATA_STALE"))
    assert recovered != stale
    assert recovered == canonical_dedupe_key(event("DATA_RECOVERED", currentPrice=9999))


def test_entry_actionability_ttl_blocks_delayed_action_signal():
    now = datetime(2026, 8, 24, 1, 6, tzinfo=timezone.utc)
    assert is_expired(event("ENTRY_READY"), now=now) is True
    assert is_expired(event("STOP_TRIGGERED"), now=now) is False


def test_sunday_reopen_has_first_candle_grace_but_later_time_does_not():
    # 2026-08-24 06:09 Taipei = Sunday 18:09 New York.
    assert _market_reopen_grace(
        datetime(2026, 8, 23, 22, 9, tzinfo=timezone.utc)) is True
    assert _market_reopen_grace(
        datetime(2026, 8, 23, 22, 21, tzinfo=timezone.utc)) is False


def test_thesis_warning_and_recovery_are_plain_language_position_events():
    base = {
        "direction": "LONG", "currentPrice": 4613.0, "warningLevel": 4615.0,
        "hardInvalidation": 4594.73,
        "tradeThesis": {"thesisDescription": "4594.73 掃低後 reclaim 多單"},
    }
    warning = format_decision_message({**base, "event_type": "POSITION_WARNING"})
    recovered = format_decision_message({**base, "event_type": "POSITION_RECOVERED"})
    assert "尚未正式失效" in warning and "禁止加碼" in warning
    assert "防守成功" in recovered and "不代表建立新進場" in recovered

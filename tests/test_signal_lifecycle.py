from app.engines.signal_lifecycle import evaluate_signal_lifecycle
from app.services.notification_policy import canonical_dedupe_key, eligibility
from app.services.current_decision_store import publish_current_final_decision


def decision(**updates):
    base = {
        "setupId": "short-824", "direction": "SHORT", "currentPrice": 4657.39,
        "entryZone": {"low": 4648.23, "high": 4651.94},
        "chaseLimit": 4645.0, "invalidationPrice": 4662.0,
        "targets": [4640.0, 4632.0], "effectiveRR": 1.2,
        "finalAction": "WAIT", "canEnter": False,
        "blockingReasons": ["RISK_REWARD_TOO_LOW"],
        "marketState": "BEARISH", "sourceCandleCloseTime": "2026-08-24T06:15:00Z",
        "calculatedAt": "2026-08-24T06:17:00Z",
    }
    base.update(updates)
    return base


def test_short_above_zone_is_not_directional_chase_and_remains_recalculated():
    state, events = evaluate_signal_lifecycle(decision())
    assert state["eventType"] == "SETUP_WEAKENING"
    assert events[0]["event_type"] == "SETUP_WEAKENING"
    assert events[0]["canEnter"] is False


def test_short_below_chase_limit_is_price_ran_away():
    state, _ = evaluate_signal_lifecycle(decision(currentPrice=4640.0, effectiveRR=2.0))
    assert state["eventType"] == "PRICE_RAN_AWAY"


def test_live_quote_can_approach_but_never_invent_entry_confirmation():
    state, events = evaluate_signal_lifecycle(
        decision(currentPrice=4652.4, effectiveRR=1.8, blockingReasons=[]),
        live_quote=True,
    )
    assert state["eventType"] == "RETRACE_APPROACHING"
    assert events[0]["canEnter"] is False


def test_same_lifecycle_signature_is_silent():
    first, events = evaluate_signal_lifecycle(decision())
    repeated, duplicate = evaluate_signal_lifecycle(decision(currentPrice=4657.40), first)
    assert events
    assert repeated["signature"] == first["signature"]
    assert duplicate == []


def test_material_zone_change_gets_new_dedupe_key_but_quote_does_not():
    payload = {
        **decision(), "event_type": "WAIT_RETRACE", "eventVersion": 1,
        "currentState": "WAIT_RETRACE", "symbol": "XAUUSD",
    }
    quote = {**payload, "currentPrice": 4659.0, "calculatedAt": "2026-08-24T06:20:00Z"}
    moved = {**payload, "entryZone": {"low": 4653.0, "high": 4656.0}}
    assert canonical_dedupe_key(payload) == canonical_dedupe_key(quote)
    assert canonical_dedupe_key(payload) != canonical_dedupe_key(moved)


def test_complete_lifecycle_events_are_notification_eligible():
    for event_type in (
        "SETUP_FORMING", "ENTRY_APPROACHING", "RETRACE_APPROACHING",
        "RETRACE_ZONE_ENTERED", "SETUP_WEAKENING", "TARGET_UPDATED",
        "TP_APPROACHING", "EXIT_WARNING", "ENTRY_INVALIDATED",
    ):
        assert eligibility({"event_type": event_type})["eligible"], event_type


def test_distinct_lifecycle_facts_do_not_collapse_to_same_event_id():
    from app.db.session import init_db
    init_db()
    base = {
        "decisionSignature": "lifecycle-event-ids", "decisionVersion": 1,
        "sourceCandleCloseTime": "2026-08-24T06:15:00Z", "sourceDataVersion": 824,
        "evaluatedAt": "2026-08-24T06:17:00Z", "finalAction": "WAIT",
        "events": [
            {"event_type": "WAIT_RETRACE", "setupId": "s1", "currentState": "WAIT_RETRACE",
             "finalDecision": "WAIT", "candleCloseTime": "2026-08-24T06:15:00Z"},
            {"event_type": "TARGET_UPDATED", "setupId": "s1", "currentState": "TARGET_UPDATED",
             "finalDecision": "WAIT", "candleCloseTime": "2026-08-24T06:15:00Z"},
        ],
    }
    published, _ = publish_current_final_decision("XAUUSD-LIFECYCLE-EVENT-ID", base)
    assert len({event["eventId"] for event in published["events"]}) == 2

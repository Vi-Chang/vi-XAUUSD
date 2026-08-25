from app.engines.final_decision_engine import _lifecycle, collect_signal_candidates


def test_not_ready_text_is_never_promoted_to_entry_ready():
    assert _lifecycle("NOT_READY") == "SETUP"
    assert _lifecycle("WAIT_ENTRY_READY_CONFIRMATION") == "SETUP"
    assert _lifecycle("ENTRY_READY") == "ENTRY_READY"
    assert _lifecycle("ENTRY_READY_RETEST") == "ENTRY_READY"
    assert _lifecycle("BREAKOUT_RETEST_READY") == "ENTRY_READY"


def test_invalid_and_missed_take_precedence_over_ready_substrings():
    assert _lifecycle("ENTRY_READY_INVALIDATED") == "INVALIDATED"
    assert _lifecycle("MISSED_ENTRY") == "MISSED"


def test_compatibility_mirrors_cannot_duplicate_one_setup_by_status():
    base = {
        "setupId": "ONE", "direction": "LONG", "signalScore": 70,
        "entryZoneLow": 100.0, "entryZoneHigh": 101.0,
        "stopPrice": 98.0, "tp1": 105.0, "type": "BREAKOUT",
    }
    data = {
        "timestamp_utc": "2026-08-25T01:00:00Z",
        "normalized_analysis": {"lastClosedCandleTimestamp": "2026-08-25T00:45:00Z"},
        "trend_continuation_engine": {
            "candidates": [{**base, "status": "WAIT_CONFIRMATION"},
                           {**base, "status": "ENTRY_READY"}],
        },
    }
    candidates = collect_signal_candidates(data)
    matching = [item for item in candidates if item.scenario_id == "ONE"]
    assert len(matching) == 1
    assert matching[0].lifecycle_state == "ENTRY_READY"

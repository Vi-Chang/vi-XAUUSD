from app.engines.entry_opportunity import evaluate_entry_opportunities
from app.engines.final_decision_engine import collect_signal_candidates
from app.services.notification_policy import canonical_dedupe_key


def payload(*, price=97.0, candle=None, target=120.0, previous_state="WATCHING"):
    setup = {
        "setupId": "setup-multizone", "direction": "LONG",
        "breakoutTrigger": 100.0, "atr15": 10.0,
        "entryZoneLow": 99.0, "entryZoneHigh": 101.0,
        "pullbackEntryZoneLow": 90.0, "pullbackEntryZoneHigh": 94.0,
        "pullbackZoneReason": ["1H 支撐", "主要高低點"],
        "stopPrice": 88.0, "tp1": target,
        "breakoutConfirmedAt": "2026-08-24T05:00:00+00:00",
        "expiresAt": "2026-08-24T08:00:00+00:00",
    }
    return {
        "symbol": "XAUUSD", "timestamp_utc": "2026-08-24T06:00:00+00:00",
        "normalized_analysis": {
            "currentPrice": price, "atr15": 10.0, "trendBias": "bullish",
            "shortTermMomentum": "recovering", "marketDataStatus": "GOOD",
            "lastClosedCandleTimestamp": "2026-08-24T05:45:00+00:00",
            "confirmationLevels": [
                {"kind": "support", "timeframe": "15M", "price": 97.0},
                {"kind": "support", "timeframe": "1H", "price": 92.0},
            ],
        },
        "latest_closed_15m": candle or {"open": 96.0, "high": 99.0,
                                         "low": 95.5, "close": 98.5},
        "breakout_setup_manager": {"activeSetup": setup},
    }


def by_type(state, kind):
    return next(item for item in state["opportunities"] if item["type"] == kind)


def test_case_a_and_f_shallow_ready_is_not_blocked_by_higher_rr_deep_zone():
    state, _ = evaluate_entry_opportunities(payload())
    shallow, deep = by_type(state, "SHALLOW_PULLBACK"), by_type(state, "DEEP_PULLBACK")
    assert shallow["state"] == "ENTRY_READY"
    assert deep["estimated_rr"] > shallow["estimated_rr"]
    assert state["primaryOpportunityId"] == shallow["opportunity_id"]
    assert state["strongTrendShallowRetraceMode"] is True


def test_case_c_and_g_zone_touch_only_shows_estimated_rr_until_closed_confirmation():
    data = payload(candle={"open": 98.0, "high": 99.0, "low": 96.0, "close": 97.0})
    state, _ = evaluate_entry_opportunities(data)
    shallow = by_type(state, "SHALLOW_PULLBACK")
    assert shallow["state"] == "WAIT_CONFIRMATION"
    assert shallow["estimated_rr"] is not None
    assert shallow["executable_rr"] is None


def test_case_b_executable_rr_below_gate_rejects_even_if_estimate_was_acceptable():
    # Confirm near the least favourable edge; estimated preview cannot authorize.
    state, _ = evaluate_entry_opportunities(payload(price=98.05, target=105.0))
    shallow = by_type(state, "SHALLOW_PULLBACK")
    assert shallow["estimated_rr"] >= 1.5
    assert shallow["executable_rr"] < 1.5
    assert shallow["state"] == "REJECTED"


def test_case_h_and_l_old_executable_rr_is_cleared_after_price_leaves_zone():
    first, _ = evaluate_entry_opportunities(payload())
    moved, _ = evaluate_entry_opportunities(payload(price=105.0), first)
    shallow = by_type(moved, "SHALLOW_PULLBACK")
    assert shallow["state"] == "MISSED"
    assert shallow["executable_rr"] is None
    assert shallow["candidate_entry"] is None


def test_case_i_only_one_primary_entry_ready():
    data = payload(price=96.0)
    # Widen the legacy deep center to overlap shallow for this collision case.
    data["breakout_setup_manager"]["activeSetup"].update(
        pullbackEntryZoneLow=95.0, pullbackEntryZoneHigh=97.0)
    state, _ = evaluate_entry_opportunities(data)
    ready = [x for x in state["opportunities"] if x["state"] == "ENTRY_READY"]
    alternatives = [x for x in state["opportunities"] if x["state"] == "ALTERNATIVE_READY"]
    assert len(ready) == 1
    assert alternatives


def test_case_d_e_stale_and_recovery_are_transition_deduped():
    stale = {"symbol": "XAUUSD", "event_type": "DATA_STALE", "eventVersion": 1,
             "currentState": "DATA_STALE", "candleCloseTime": "2026-08-24T06:00:00Z"}
    repeated = {**stale, "candleCloseTime": "2026-08-24T06:15:00Z", "currentPrice": 9999}
    recovered = {**stale, "event_type": "DATA_RECOVERED", "currentState": "DATA_RECOVERED"}
    assert canonical_dedupe_key(stale) == canonical_dedupe_key(repeated)
    assert canonical_dedupe_key(stale) != canonical_dedupe_key(recovered)


def test_case_j_new_structure_creates_new_opportunities_and_expires_old_identity():
    first, _ = evaluate_entry_opportunities(payload())
    changed = payload()
    changed["breakout_setup_manager"]["activeSetup"].update(
        setupId="setup-new-structure", breakoutTrigger=110.0)
    second, _ = evaluate_entry_opportunities(changed, first)
    assert second["setupId"] == "setup-new-structure"
    assert {x["opportunity_id"] for x in first["opportunities"]}.isdisjoint(
        {x["opportunity_id"] for x in second["opportunities"]})
    assert second["archivedOpportunities"]
    assert all(x["state"] == "EXPIRED" for x in second["archivedOpportunities"])


def test_case_k_wait_retrace_polling_has_stable_notification_key():
    event = {"symbol": "XAUUSD", "setupId": "s1", "opportunityId": "o1",
             "event_type": "WAIT_RETRACE", "currentState": "WAIT_RETRACE",
             "entryZone": {"low": 95.0, "high": 98.0}, "eventVersion": 1}
    ten_minutes_later = {**event, "currentPrice": 105.0,
                         "calculatedAt": "2026-08-24T06:10:00Z"}
    assert canonical_dedupe_key(event) == canonical_dedupe_key(ten_minutes_later)


def test_estimated_rr_never_enters_final_trade_permission_candidate():
    opportunity = {
        "opportunity_id": "o-preview", "setup_id": "s1", "type": "SHALLOW_PULLBACK",
        "side": "LONG", "entry_zone": {"lower": 95.0, "upper": 98.0},
        "state": "WAIT_CONFIRMATION", "estimated_rr": 2.2, "executable_rr": None,
        "tactical_stop": 90.0, "target1": 110.0, "opportunity_score": 90,
    }
    candidates = collect_signal_candidates({
        "entry_opportunity_engine": {"opportunities": [opportunity]},
        "normalized_analysis": {},
    })
    assert len(candidates) == 1
    assert candidates[0].estimated_risk_reward == 2.2
    assert candidates[0].risk_reward is None
    assert candidates[0].lifecycle_state != "ENTRY_READY"


def test_failed_break_reclaim_requires_next_closed_hold_before_entry_ready():
    data = payload()
    data["break_lifecycle_engine"] = {"state": "FAILED_BREAKDOWN", "level": 97.0}
    state, _ = evaluate_entry_opportunities(data)
    shallow = by_type(state, "SHALLOW_PULLBACK")
    assert shallow["reclaim_confirmation_required"] is True
    assert shallow["state"] != "ENTRY_READY"
    assert shallow["executable_rr"] is None


def continuation_payload(*, price: float, candle: dict,
                         timestamp: str = "2026-08-24T06:00:00+00:00") -> dict:
    data = payload(price=price, candle=candle, target=4720.0)
    data["timestamp_utc"] = timestamp
    setup = data["breakout_setup_manager"]["activeSetup"]
    setup.update({
        "breakoutTrigger": 4655.60,
        "entryZoneLow": 4655.60,
        "entryZoneHigh": 4659.34,
        "pullbackEntryZoneLow": 4646.99,
        "pullbackEntryZoneHigh": 4650.72,
        "atr15": 10.0,
    })
    normalized = data["normalized_analysis"]
    normalized.update({
        "currentPrice": price,
        "atr15": 10.0,
        "timeframeAssessments": [
            {"timeframe": "1H", "trend": "bullish"},
            {"timeframe": "4H", "trend": "bullish"},
        ],
        "confirmationLevels": [
            {"kind": "support", "timeframe": "15M", "price": 4650.85},
            {"kind": "support", "timeframe": "1H", "price": 4648.0},
            {"kind": "resistance", "timeframe": "15M", "price": 4696.75},
        ],
    })
    return data


def test_stale_entry_anchor_becomes_deep_backup_not_primary_trigger():
    data = continuation_payload(
        price=4693.0,
        candle={"open": 4680.0, "high": 4694.0, "low": 4678.0, "close": 4681.47},
    )
    state, _ = evaluate_entry_opportunities(data)
    primary = next(item for item in state["opportunities"]
                   if item["opportunity_id"] == state["primaryOpportunityId"])
    assert primary["type"] == "BREAKOUT_RETEST"
    old_zones = [item for item in state["opportunities"]
                 if (item["entry_zone"]["upper"] or 0) < 4660]
    assert old_zones
    assert all(item["anchor_role"] == "DEEP_PULLBACK_BACKUP"
               and item["primary_eligible"] is False for item in old_zones)
    canonical_candidates = collect_signal_candidates({
        **data, "entry_opportunity_engine": state,
    })
    assert canonical_candidates
    assert all(not (candidate.entry_zone and candidate.entry_zone[1] < 4660)
               for candidate in canonical_candidates)


def test_breakout_continuation_requires_retest_then_becomes_entry_ready():
    breakout = continuation_payload(
        price=4699.0,
        candle={"open": 4692.0, "high": 4700.0, "low": 4691.0, "close": 4698.5},
    )
    first, _ = evaluate_entry_opportunities(breakout)
    candidate = by_type(first, "BREAKOUT_RETEST")
    assert candidate["state"] == "WAIT_BREAKOUT_RETEST"
    assert candidate["entry_type"] == "BREAKOUT_RETEST"

    retest = continuation_payload(
        price=4697.0,
        timestamp="2026-08-24T06:15:00+00:00",
        candle={"open": 4697.0, "high": 4699.0, "low": 4692.0, "close": 4697.2},
    )
    retest["normalized_analysis"]["lastClosedCandleTimestamp"] = (
        "2026-08-24T06:00:00+00:00")
    second, _ = evaluate_entry_opportunities(retest, first)
    ready = by_type(second, "BREAKOUT_RETEST")
    assert ready["state"] == "ENTRY_READY"
    assert ready["entry_type"] == "BREAKOUT_RETEST"
    assert ready["executable_rr"] >= 1.5
    canonical_candidates = collect_signal_candidates({
        **retest, "entry_opportunity_engine": second,
    })
    assert any(candidate.lifecycle_state == "ENTRY_READY"
               and candidate.setup_type == "BREAKOUT_RETEST"
               for candidate in canonical_candidates)


def test_breakout_without_later_retest_never_grants_buy_now():
    data = continuation_payload(
        price=4699.0,
        candle={"open": 4692.0, "high": 4700.0, "low": 4691.0, "close": 4698.5},
    )
    state, _ = evaluate_entry_opportunities(data)
    candidate = by_type(state, "BREAKOUT_RETEST")
    assert candidate["state"] == "WAIT_BREAKOUT_RETEST"
    assert candidate["executable_rr"] is None

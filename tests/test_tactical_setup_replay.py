from app.engines.tactical_setup import classify_tactical_setup


BASE = {
    "weakness_state": "confirmed",
    "weakness_families": ["price_structure", "momentum", "oscillator"],
    "trend_bias": "bullish",
    "support": 4370.02,
    "buffer": 1.2,
    "atr15": 8.0,
    "last_closed_at": "2026-08-13T16:00:00+00:00",
    "rr_to_next_support": 2.0,
}


def test_aug12_single_closed_breach_does_not_short():
    setup = classify_tactical_setup(
        **BASE, support_state="testing_support", current_price=4369.2)
    assert setup.setup_state == "OBSERVE"
    assert setup.tactical_bias == "neutral"
    assert "第二根" in setup.missing_condition


def test_aug12_reclaim_cancels_false_break_short():
    setup = classify_tactical_setup(
        **BASE, support_state="failed_breakdown", current_price=4374.5)
    assert setup.setup_state == "OBSERVE"
    assert setup.tactical_bias == "neutral"


def test_aug13_confirmed_breakdown_becomes_short_watch_despite_bullish_4h():
    setup = classify_tactical_setup(
        **BASE, support_state="confirmed_breakdown", current_price=4364.18)
    assert setup.setup_state == "SHORT_WATCH"
    assert setup.tactical_bias == "bearish"
    assert setup.trigger_level == 4370.02
    assert setup.next_check_time == "2026-08-13T16:30:00+00:00"
    assert setup.expires_at == "2026-08-13T17:15:00+00:00"
    assert "不否決短空" in setup.message


def test_aug13_failed_retest_with_rr_becomes_short_ready():
    setup = classify_tactical_setup(
        **BASE, support_state="retest_rejected", current_price=4368.8,
        retest_failed=True)
    assert setup.setup_state == "SHORT_READY"
    assert setup.invalidation_level == 4371.22


def test_aug13_extended_drop_is_no_chase():
    setup = classify_tactical_setup(
        **BASE, support_state="confirmed_breakdown", current_price=4349.26)
    assert setup.setup_state == "NO_CHASE"
    assert "不追空" in setup.message


def test_correlated_momentum_family_does_not_count_twice():
    setup = classify_tactical_setup(
        **{**BASE, "weakness_families": ["momentum", "momentum"]},
        support_state="confirmed_breakdown", current_price=4364.18)
    assert setup.setup_state == "OBSERVE"

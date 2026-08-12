from app.services.performance_service import MIN_SAMPLE_SIZE, _score_band, _session, _summary


def test_summary_marks_small_sample_insufficient():
    out = _summary([1.0, -0.5, 0.25])
    assert out["sample_size"] == 3
    assert out["sufficient_sample"] is False
    assert out["win_rate_pct"] == 66.7


def test_summary_marks_minimum_sample_sufficient():
    out = _summary([0.1] * MIN_SAMPLE_SIZE)
    assert out["sufficient_sample"] is True
    assert out["win_rate_pct"] == 100.0


def test_score_bands_are_bounded():
    assert _score_band(0) == "0-9"
    assert _score_band(67) == "60-69"
    assert _score_band(100) == "90-99"


def test_sessions_use_utc_hours():
    assert _session(2) == "ASIA"
    assert _session(9) == "LONDON"
    assert _session(15) == "NEW_YORK"
    assert _session(22) == "OFF_HOURS"

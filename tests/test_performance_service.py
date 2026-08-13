from app.services.performance_service import (
    MIN_SAMPLE_SIZE,
    _calibration_recommendations,
    _score_band,
    _session,
    _summary,
    _walk_forward_validation,
)


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


def test_calibration_recommendations_need_sufficient_sample():
    groups = {"overall": [{"key": "ALL", "horizon": "1h", "sample_size": 29, "sufficient_sample": False, "average_mae_pct": -1.0, "average_planned_risk_pct": 1.0, "average_mfe_pct": 0.2, "average_target_distance_pct": 1.0}], "score_band": [], "session": [], "market_state": []}
    assert _calibration_recommendations(groups) == []


def test_calibration_recommendations_are_review_only():
    groups = {"overall": [{"key": "ALL", "horizon": "1h", "sample_size": 30, "sufficient_sample": True, "average_mae_pct": -0.9, "average_planned_risk_pct": 1.0, "average_mfe_pct": 0.6, "average_target_distance_pct": 1.0}], "score_band": [{"key": "70-79", "horizon": "1h", "sample_size": 30, "sufficient_sample": True, "win_rate_pct": 40.0, "average_return_pct": -0.1}], "session": [], "market_state": []}
    recommendations = _calibration_recommendations(groups)
    assert {item["kind"] for item in recommendations} == {"stop_buffer_review", "target_distance_review", "signal_filter_review"}
    assert all(item["review_required"] is True for item in recommendations)


def test_walk_forward_requires_two_independent_sample_windows():
    insufficient = _walk_forward_validation({"1h": [0.1] * 59})
    assert insufficient["1h"]["status"] == "not_validated"
    validated = _walk_forward_validation({"1h": [0.1] * 30 + [0.2] * 30})
    assert validated["1h"]["status"] == "validated"
    conflicting = _walk_forward_validation({"1h": [0.1] * 30 + [-0.2] * 30})
    assert conflicting["1h"]["status"] == "not_validated"

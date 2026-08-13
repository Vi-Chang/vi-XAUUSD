from app.schemas.analysis import AnalysisResult
from app.services.calibration_guard import apply_calibration_guard


def test_insufficient_samples_cap_high_confidence_without_changing_action():
    result = AnalysisResult()
    result.decision.action = "PREPARE_LONG"
    result.decision.confidence_grade = "A"
    result.decision.reason = "技術條件成立。"

    apply_calibration_guard(result, sample_size=6, minimum=30)

    assert result.decision.action == "PREPARE_LONG"
    assert result.decision.confidence_grade == "B"
    assert result.calibration_status == "collecting"
    assert result.calibration_sample_size == 6
    assert "6/30" in result.calibration_message


def test_sufficient_samples_preserve_confidence():
    result = AnalysisResult()
    result.decision.confidence_grade = "A"

    apply_calibration_guard(result, sample_size=30, minimum=30)

    assert result.decision.confidence_grade == "A"
    assert result.calibration_status == "sufficient"


def test_insufficient_samples_do_not_downgrade_existing_conservative_grade():
    result = AnalysisResult()
    result.decision.confidence_grade = "C"

    apply_calibration_guard(result, sample_size=0, minimum=30)

    assert result.decision.confidence_grade == "C"

import json
from pathlib import Path

from app.engines.position_calibration import calibration_report


def test_representative_history_calibration_reduces_false_exits():
    path = Path(__file__).parent / "fixtures" / "position_risk_calibration.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    report = calibration_report(cases)

    assert report["sampleSize"] == 7
    assert report["falseExitRate"] == 0.0
    assert report["legacyFalseExitRate"] == 0.6
    assert report["missedConfirmedExitRate"] == 0.0
    assert set(report["horizonMeanReturns"]) == {"1", "2", "4", "8"}
    assert report["autoTuning"] is False

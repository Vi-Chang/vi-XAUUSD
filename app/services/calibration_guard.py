"""Keep confidence claims proportional to settled historical evidence."""
from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import AnalysisRun


ACTIONABLE = ("LONG", "SHORT", "PREPARE_LONG", "PREPARE_SHORT")


def settled_sample_size(db) -> int:
    value = db.execute(
        select(func.count(AnalysisRun.id)).where(
            AnalysisRun.decision_action.in_(ACTIONABLE),
            AnalysisRun.outcome_1h.is_not(None),
        )
    ).scalar_one()
    return int(value or 0)


def apply_calibration_guard(result, *, sample_size: int, minimum: int) -> None:
    """Cap confidence without changing direction or entry levels."""
    result.calibration_sample_size = max(0, sample_size)
    result.calibration_min_sample_size = max(1, minimum)
    if sample_size >= minimum:
        result.calibration_status = "sufficient"
        result.calibration_message = f"歷史驗證樣本 {sample_size} 筆，已達最低門檻。"
        return
    result.calibration_status = "collecting"
    result.calibration_message = (
        f"歷史驗證樣本 {sample_size}/{minimum}，技術方向可參考，"
        "但進場信心仍在累積。"
    )
    if result.decision.confidence_grade in ("S", "A"):
        result.decision.confidence_grade = "B"
        result.decision.reason = (
            result.decision.reason.rstrip("。")
            + "；歷史驗證樣本不足，高信心暫時降級。"
        )
    result.market_decision = result.decision.model_copy()

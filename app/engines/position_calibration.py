"""持倉風險規則的可重現回放校準統計。"""
from __future__ import annotations

from collections.abc import Iterable


def calibration_report(cases: Iterable[dict]) -> dict:
    rows = list(cases)
    false_exits_new = false_exits_legacy = expected_holds = 0
    false_blocks = 0
    horizon = {1: [], 2: [], 4: [], 8: []}
    for case in rows:
        expected_exit = bool(case.get("expected_exit"))
        context_complete = bool(case.get("context_complete"))
        confirmed = bool(case.get("confirmed_invalidation"))
        weakness = case.get("weakness")
        new_exit = context_complete and confirmed
        legacy_exit = weakness == "accelerating"
        if not expected_exit:
            expected_holds += 1
            false_exits_new += int(new_exit)
            false_exits_legacy += int(legacy_exit)
        false_blocks += int(expected_exit and not new_exit)
        returns = list(case.get("returns", []))
        for index, bars in enumerate((1, 2, 4, 8)):
            if index < len(returns):
                horizon[bars].append(float(returns[index]))
    count = len(rows)
    return {
        "sampleSize": count,
        "falseExitRate": round(false_exits_new / expected_holds, 4) if expected_holds else 0.0,
        "legacyFalseExitRate": round(false_exits_legacy / expected_holds, 4) if expected_holds else 0.0,
        "missedConfirmedExitRate": round(false_blocks / count, 4) if count else 0.0,
        "averageMAE": round(sum(float(x["mae"]) for x in rows) / count, 2) if count else 0.0,
        "averageMFE": round(sum(float(x["mfe"]) for x in rows) / count, 2) if count else 0.0,
        "horizonMeanReturns": {
            str(k): round(sum(values) / len(values), 4) if values else None
            for k, values in horizon.items()
        },
        "autoTuning": False,
        "note": "校準樣本只評估規則品質，不代表未來勝率或方向預測。",
    }

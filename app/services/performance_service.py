"""Aggregate anonymous rule-signal performance from persisted outcomes."""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from app.db.models import AnalysisRun

HORIZONS = ("outcome_15m", "outcome_1h", "outcome_4h", "outcome_1d")
MIN_SAMPLE_SIZE = 30


def _direction(action: str) -> str | None:
    if action in ("LONG", "PREPARE_LONG"):
        return "LONG"
    if action in ("SHORT", "PREPARE_SHORT"):
        return "SHORT"
    return None


def _score_band(score: int) -> str:
    floor = max(0, min(90, (score // 10) * 10))
    return f"{floor}-{floor + 9}"


def _session(hour: int) -> str:
    if hour < 7:
        return "ASIA"
    if hour < 13:
        return "LONDON"
    if hour < 21:
        return "NEW_YORK"
    return "OFF_HOURS"


def _summary(values: list[float]) -> dict:
    n = len(values)
    return {"sample_size": n, "sufficient_sample": n >= MIN_SAMPLE_SIZE,
            "win_rate_pct": round(100 * sum(v > 0 for v in values) / n, 1) if n else None,
            "average_return_pct": round(sum(values) / n, 5) if n else None}


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 5) if values else None


def _midpoint(price: object) -> float | None:
    """Return the midpoint of a resolved price zone, when it is usable."""
    if not isinstance(price, dict):
        return None
    low, high = price.get("price_low"), price.get("price_high")
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return (float(low) + float(high)) / 2


def _planned_distances(row: AnalysisRun) -> tuple[float | None, float | None]:
    """Extract planned stop and first-target distances without changing a signal."""
    direction = _direction(row.decision_action)
    result = row.result_json or {}
    scenario = result.get("long_scenario" if direction == "LONG" else "short_scenario")
    if direction is None or not isinstance(scenario, dict):
        return None, None
    prices = scenario.get("resolved_prices") or {}
    entry = _midpoint(prices.get(scenario.get("entry_zone_id")))
    stop = _midpoint(prices.get(scenario.get("stop_loss_id")))
    target_ids = scenario.get("target_ids") or []
    target = _midpoint(prices.get(target_ids[0])) if target_ids else None
    if entry is None or entry <= 0:
        return None, None
    risk = abs(entry - stop) / entry * 100 if stop is not None and stop != entry else None
    reward = abs(target - entry) / entry * 100 if target is not None and target != entry else None
    return risk, reward


def _calibration_recommendations(groups: dict[str, list[dict]]) -> list[dict]:
    """Return review-only prompts; these never alter live thresholds."""
    recommendations: list[dict] = []
    for row in groups.get("overall", []):
        if not row["sufficient_sample"]:
            continue
        mae, risk = row.get("average_mae_pct"), row.get("average_planned_risk_pct")
        if isinstance(mae, (int, float)) and isinstance(risk, (int, float)) and risk > 0 and abs(mae) >= risk * 0.8:
            recommendations.append({"kind": "stop_buffer_review", "scope": "ALL", "horizon": row["horizon"], "severity": "review", "sample_size": row["sample_size"], "review_required": True, "message": f"平均逆行 {abs(mae):.3f}% 已接近平均計畫風險 {risk:.3f}%，建議人工檢視停損緩衝；系統不會自動調整。"})
        mfe, target = row.get("average_mfe_pct"), row.get("average_target_distance_pct")
        if isinstance(mfe, (int, float)) and isinstance(target, (int, float)) and target > 0 and mfe <= target * 0.7:
            recommendations.append({"kind": "target_distance_review", "scope": "ALL", "horizon": row["horizon"], "severity": "review", "sample_size": row["sample_size"], "review_required": True, "message": f"平均順行 {mfe:.3f}% 低於第一目標距離 {target:.3f}% 的 70%，建議人工檢視目標設定；系統不會自動調整。"})
    for group in ("score_band", "session", "market_state"):
        for row in groups.get(group, []):
            if not row["sufficient_sample"]:
                continue
            win_rate, average_return = row.get("win_rate_pct"), row.get("average_return_pct")
            if (isinstance(win_rate, (int, float)) and win_rate < 45) or (isinstance(average_return, (int, float)) and average_return <= 0):
                recommendations.append({"kind": "signal_filter_review", "scope": f"{group}:{row['key']}", "horizon": row["horizon"], "severity": "review", "sample_size": row["sample_size"], "review_required": True, "message": f"此分組勝率 {win_rate:.1f}%、平均報酬 {average_return:.3f}% 表現偏弱，建議人工檢視是否降低信心或改為等待；系統不會自動調整。"})
    return recommendations[:8]


def _walk_forward_validation(values_by_horizon: dict[str, list[float]]) -> dict[str, dict]:
    """Split chronologically ordered outcomes into calibration and holdout windows."""
    result: dict[str, dict] = {}
    for horizon, values in values_by_horizon.items():
        midpoint = len(values) // 2
        calibration, validation = values[:midpoint], values[midpoint:]
        calibration_summary, validation_summary = _summary(calibration), _summary(validation)
        calibration_return = calibration_summary["average_return_pct"]
        validation_return = validation_summary["average_return_pct"]
        enough = calibration_summary["sufficient_sample"] and validation_summary["sufficient_sample"]
        agrees = (isinstance(calibration_return, (int, float)) and isinstance(validation_return, (int, float))
                  and ((calibration_return > 0 and validation_return > 0)
                       or (calibration_return <= 0 and validation_return <= 0)))
        result[horizon] = {"calibration": calibration_summary, "validation": validation_summary,
                           "status": "validated" if enough and agrees else "not_validated",
                           "reason": "兩段樣本的平均報酬方向一致" if enough and agrees
                           else "樣本不足或校正期與樣本外驗證期表現不一致"}
    return result


def performance_report(db, *, limit: int = 5000) -> dict:
    rows = db.execute(select(AnalysisRun).order_by(AnalysisRun.run_time.desc()).limit(limit)).scalars().all()
    buckets = {name: defaultdict(list) for name in
               ("overall", "direction", "score_band", "market_state", "session")}
    excursions = {name: defaultdict(lambda: {"mfe": [], "mae": []}) for name in buckets}
    planned = {name: defaultdict(lambda: {"risk": [], "target": []}) for name in buckets}
    outcomes_by_horizon: dict[str, list[float]] = defaultdict(list)
    eligible = 0
    for row in rows:
        direction = _direction(row.decision_action)
        if direction is None:
            continue
        eligible += 1
        keys = {"overall": "ALL", "direction": direction,
                "score_band": _score_band(row.evidence_score),
                "market_state": row.market_state, "session": _session(row.run_time.hour)}
        planned_risk, planned_target = _planned_distances(row)
        for horizon in HORIZONS:
            value = getattr(row, horizon)
            if value is None:
                continue
            outcomes_by_horizon[horizon.removeprefix("outcome_")].append(float(value))
            for group, key in keys.items():
                buckets[group][f"{key}|{horizon}"].append(float(value))
                path = ((row.result_json or {}).get("outcome_path") or {}).get(horizon) or {}
                if isinstance(path.get("mfe_pct"), (int, float)):
                    excursions[group][f"{key}|{horizon}"]["mfe"].append(float(path["mfe_pct"]))
                if isinstance(path.get("mae_pct"), (int, float)):
                    excursions[group][f"{key}|{horizon}"]["mae"].append(float(path["mae_pct"]))
                if planned_risk is not None:
                    planned[group][f"{key}|{horizon}"]["risk"].append(planned_risk)
                if planned_target is not None:
                    planned[group][f"{key}|{horizon}"]["target"].append(planned_target)
    groups = {}
    for group, entries in buckets.items():
        groups[group] = [{"key": combined.rsplit("|", 1)[0],
                          "horizon": combined.rsplit("|", 1)[1].removeprefix("outcome_"),
                          **_summary(values),
                          "average_mfe_pct": _average(excursions[group][combined]["mfe"]),
                          "average_mae_pct": _average(excursions[group][combined]["mae"]),
                          "average_planned_risk_pct": _average(planned[group][combined]["risk"]),
                          "average_target_distance_pct": _average(planned[group][combined]["target"])}
                         for combined, values in sorted(entries.items())]
    walk_forward = _walk_forward_validation({key: list(reversed(values)) for key, values in outcomes_by_horizon.items()})
    recommendations = _calibration_recommendations(groups)
    for recommendation in recommendations:
        validation = walk_forward.get(recommendation["horizon"], {})
        recommendation["walk_forward_status"] = validation.get("status", "not_validated")
        recommendation["walk_forward_reason"] = validation.get("reason", "尚無樣本外驗證資料")
    return {"eligible_signals": eligible, "minimum_sample_size": MIN_SAMPLE_SIZE,
            "auto_tuning_enabled": False, "groups": groups,
            "calibration_recommendations": recommendations, "walk_forward_validation": walk_forward}

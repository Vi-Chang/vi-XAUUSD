"""Point-in-time comparison of stop policies; never fabricates missing history."""

from __future__ import annotations

from statistics import mean, median

from app.engines.thesis_invalidation import (
    build_trade_thesis,
    evaluate_invalidation,
    initial_invalidation_state,
)

POLICIES = ("SINGLE_PRICE", "CLOSE_BASED", "THESIS_BASED")


def _adverse(direction: str, value: float, level: float) -> bool:
    return value <= level if direction == "LONG" else value >= level


def compare_case(case: dict) -> list[dict]:
    """Replay bars in order using only values known at each candle."""
    thesis = build_trade_thesis(case, created_at=str(case["created_at"]))
    direction, entry = str(case["direction"]), float(case["suggested_entry"])
    sign = 1 if direction == "LONG" else -1
    warning = float(thesis["warningLevel"])
    risk = float(thesis["stopDistance"])
    bars = list(case.get("bars") or [])
    results = []
    for policy in POLICIES:
        state = initial_invalidation_state(thesis)
        exit_price, exit_index, exit_reason = None, None, "END_OF_REPLAY"
        mae = mfe = 0.0
        for index, bar in enumerate(bars):
            low, high, close = map(float, (bar["low"], bar["high"], bar["close"]))
            adverse = entry - low if direction == "LONG" else high - entry
            favorable = high - entry if direction == "LONG" else entry - low
            mae, mfe = max(mae, adverse), max(mfe, favorable)
            if policy == "SINGLE_PRICE" and _adverse(
                    direction, low if direction == "LONG" else high, warning):
                exit_price, exit_index, exit_reason = warning, index, "PRICE_TOUCH"
            elif policy == "CLOSE_BASED" and _adverse(direction, close, warning):
                exit_price, exit_index, exit_reason = close, index, "CLOSE_BREAK"
            elif policy == "THESIS_BASED":
                current = low if direction == "LONG" else high
                state, _ = evaluate_invalidation(
                    thesis, state, current_price=current, closed_price=close,
                    candle_close_time=str(bar["time"]), atr15=float(case.get("atr15") or risk),
                    regime=str(case.get("regime") or ""), data_status="GOOD")
                if state["state"] in {"SOFT_INVALIDATED", "HARD_INVALIDATED"}:
                    exit_price, exit_index, exit_reason = close, index, state["reasonCode"]
            if exit_price is not None:
                break
        final = float(bars[-1]["close"]) if bars else entry
        realized = sign * ((exit_price if exit_price is not None else final) - entry) / risk
        later_reached_one_r = False
        if exit_index is not None:
            later = bars[exit_index + 1:]
            later_reached_one_r = any(
                (float(bar["high"]) >= entry + risk if direction == "LONG"
                 else float(bar["low"]) <= entry - risk) for bar in later)
        results.append({
            "caseId": case.get("case_id"), "strategyType": thesis["strategyType"],
            "policy": policy, "exitReason": exit_reason,
            "exitIndex": exit_index, "resultR": round(realized, 4),
            "maeR": round(mae / risk, 4), "mfeR": round(mfe / risk, 4),
            "falseStop": bool(exit_index is not None and later_reached_one_r),
        })
    return results


def aggregate_comparison(cases: list[dict], *, minimum_sample: int = 30) -> dict:
    rows = [row for case in cases for row in compare_case(case)]
    strategies = sorted({str(row["strategyType"]) for row in rows})
    output: dict[str, dict[str, dict[str, object]]] = {}
    for strategy in strategies:
        output[strategy] = {}
        for policy in POLICIES:
            subset = [row for row in rows if row["strategyType"] == strategy
                      and row["policy"] == policy]
            n = len(subset)
            values = [float(row["resultR"]) for row in subset]
            output[strategy][policy] = {
                "sampleSize": n,
                "status": "VERIFIED" if n >= minimum_sample else "INSUFFICIENT_SAMPLE",
                "winRate": round(sum(value > 0 for value in values) / n, 4) if n else None,
                "averageR": round(mean(values), 4) if values else None,
                "medianR": round(median(values), 4) if values else None,
                "falseStopRate": round(sum(row["falseStop"] for row in subset) / n, 4)
                if n else None,
                "medianMaeR": round(median([row["maeR"] for row in subset]), 4)
                if subset else None,
                "medianMfeR": round(median([row["mfeR"] for row in subset]), 4)
                if subset else None,
            }
    return {
        "sampleCount": len(cases), "minimumSample": minimum_sample,
        "verified": bool(cases) and all(
            metrics[policy]["status"] == "VERIFIED"
            for metrics in output.values() for policy in POLICIES),
        "strategies": output, "rows": rows,
        "researchVersion": "thesis-risk-research-v1",
    }

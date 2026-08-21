"""Persist reproducible final-decision snapshots and summarize shadow outcomes."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import DecisionReplay
from app.db.session import db_session


def _replay_payload(data: dict, decision: dict) -> dict:
    return {
        "candles": data.get("replay_candles") or {},
        "indicators": data.get("indicators") or {},
        "levels": (data.get("normalized_analysis") or {}).get("confirmationLevels") or [],
        "regime": data.get("regime_state_machine") or {},
        "candidates": decision.get("signalCandidates") or [],
        "riskGate": decision.get("riskGate"),
        "rawScore": decision.get("rawScore"),
        "calibratedProbability": decision.get("calibratedProbability"),
        "finalDecision": {
            "decisionId": decision.get("decisionId"),
            "decisionVersion": decision.get("decisionVersion"),
            "action": decision.get("finalAction"),
            "primaryReason": decision.get("primaryReason"),
            "secondaryReasons": decision.get("secondaryReasons") or [],
            "humanSummary": decision.get("humanSummary"),
        },
        "notification": (decision.get("events") or [None])[0],
        "marketDataTimestamp": (data.get("normalized_analysis") or {}).get(
            "marketDataTimestamp"),
        "sourceCandleCloseTime": (data.get("normalized_analysis") or {}).get(
            "lastClosedCandleTimestamp"),
    }


def persist_decision_replay(symbol: str, data: dict, decision: dict) -> None:
    decision_id = str(decision.get("decisionId") or "")
    if not decision_id:
        return
    with db_session() as db:
        if db.execute(select(DecisionReplay.id).where(
                DecisionReplay.decision_id == decision_id)).scalar_one_or_none():
            return
        db.add(DecisionReplay(
            decision_id=decision_id, symbol=symbol,
            decision_version=int(decision.get("decisionVersion") or 0),
            final_action=str(decision.get("finalAction") or "WAIT"),
            scenario_type=str(decision.get("selectedSetupType") or "OTHER"),
            raw_score=decision.get("rawScore"),
            calibrated_probability=decision.get("calibratedProbability"),
            payload=_replay_payload(data, decision), outcome={},
            created_at=datetime.now(timezone.utc),
        ))


def replay_performance(db, *, limit: int = 5000) -> dict:
    """Balance false positives with opportunities suppressed by risk filters."""
    rows = db.execute(select(DecisionReplay).order_by(
        DecisionReplay.created_at.desc()).limit(limit)).scalars().all()
    by_setup: dict[str, dict] = {}
    totals = {"missed_long_move": 0, "missed_short_move": 0,
              "blocked_good_trade": 0, "overly_strict_filter": 0,
              "entry_then_stop": 0, "fake_breakout": 0,
              "bad_rr_entry": 0, "overextended_entry": 0, "late_entry": 0}
    for row in rows:
        outcome = row.outcome or {}
        for key in totals:
            totals[key] += int(bool(outcome.get(key)))
        bucket = by_setup.setdefault(row.scenario_type or "OTHER", {
            "sample_size": 0, "wins": 0, "net_r": [], "mfe": [], "mae": [],
            "missed": 0, "failures": 0,
        })
        bucket["sample_size"] += 1
        net_r = outcome.get("net_r")
        if isinstance(net_r, (int, float)):
            bucket["net_r"].append(float(net_r))
            bucket["wins"] += int(net_r > 0)
        if isinstance(outcome.get("mfe_r"), (int, float)):
            bucket["mfe"].append(float(outcome["mfe_r"]))
        if isinstance(outcome.get("mae_r"), (int, float)):
            bucket["mae"].append(float(outcome["mae_r"]))
        bucket["missed"] += int(bool(outcome.get("missed_opportunity")))
        bucket["failures"] += int(bool(outcome.get("entry_then_stop")))
    report = {}
    for name, values in by_setup.items():
        settled = len(values["net_r"])
        report[name] = {
            "sample_size": values["sample_size"],
            "win_rate_pct": round(100 * values["wins"] / settled, 1) if settled else None,
            "expectancy_r": round(sum(values["net_r"]) / settled, 3) if settled else None,
            "average_rr": round(sum(values["net_r"]) / settled, 3) if settled else None,
            "average_mfe_r": round(sum(values["mfe"]) / len(values["mfe"]), 3) if values["mfe"] else None,
            "average_mae_r": round(sum(values["mae"]) / len(values["mae"]), 3) if values["mae"] else None,
            "failure_rate_pct": round(100 * values["failures"] / values["sample_size"], 1),
            "missed_rate_pct": round(100 * values["missed"] / values["sample_size"], 1),
        }
    return {"sample_size": len(rows), "missedOpportunity": totals,
            "bySetup": report,
            "adaptivePolicy": "recommendation_only_shadow_first"}

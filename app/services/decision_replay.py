"""Persist reproducible final-decision snapshots and summarize shadow outcomes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Candle, DecisionReplay
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
        "currentPrice": (data.get("normalized_analysis") or {}).get("currentPrice"),
        "atr15": (data.get("normalized_analysis") or {}).get("atr15") or data.get("atr15"),
        "selectedScenarioId": decision.get("selectedScenarioId"),
        "selectedLineageId": decision.get("selectedLineageId"),
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


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _selected_candidate(payload: dict) -> dict:
    scenario_id = payload.get("selectedScenarioId")
    candidates = payload.get("candidates") or []
    return next((item for item in candidates
                 if item.get("scenario_id") == scenario_id), candidates[0] if candidates else {})


def backfill_decision_replay_outcomes(db, *, now: datetime,
                                      lookback_days: int = 30,
                                      limit: int = 5000) -> int:
    """Evaluate final decisions using only candles received after the decision."""
    oldest = now - timedelta(days=max(2, lookback_days))
    rows = db.execute(select(DecisionReplay).where(
        DecisionReplay.created_at >= oldest,
    ).order_by(DecisionReplay.created_at.asc()).limit(limit)).scalars().all()
    changed = 0
    for row in rows:
        start = _utc(row.created_at)
        end = min(now, start + timedelta(hours=4))
        candles = db.execute(select(Candle).where(
            Candle.symbol == row.symbol, Candle.timeframe == "15M",
            Candle.is_closed.is_(True), Candle.close_time > start,
            Candle.close_time <= end,
        ).order_by(Candle.close_time.asc())).scalars().all()
        candles = [c for c in candles if _utc(c.received_at) >= start]
        if not candles:
            continue
        existing = row.outcome or {}
        if existing.get("evaluatedThrough") == _utc(candles[-1].close_time).isoformat():
            continue
        payload = row.payload or {}
        selected = _selected_candidate(payload)
        direction = str(selected.get("direction") or "")
        if direction not in {"LONG", "SHORT"}:
            regime = str((payload.get("regime") or {}).get("compositeRegime") or "")
            direction = "SHORT" if "BEAR" in regime else "LONG"
        sign = 1 if direction == "LONG" else -1
        entry = payload.get("currentPrice")
        zone = selected.get("entry_zone") or []
        if entry is None and len(zone) == 2:
            entry = (float(zone[0]) + float(zone[1])) / 2
        if entry is None:
            continue
        entry = float(entry)
        stop = selected.get("invalidation_price")
        risk = abs(entry - float(stop)) if isinstance(stop, (int, float)) else None
        if not risk:
            atr = payload.get("atr15")
            risk = float(atr) if isinstance(atr, (int, float)) and atr > 0 else None
        if not risk:
            continue
        favorable = max(sign * (float(c.high if sign > 0 else c.low) - entry)
                        for c in candles)
        adverse = min(sign * (float(c.low if sign > 0 else c.high) - entry)
                      for c in candles)
        last_move = sign * (float(candles[-1].close) - entry)
        mfe_r, mae_r, net_r = favorable / risk, adverse / risk, last_move / risk
        waiting = row.final_action in {"WAIT", "NO_TRADE"}
        missed = waiting and mfe_r >= 1 and mae_r > -1
        blocked = missed and str((payload.get("finalDecision") or {}).get(
            "primaryReason") or "") in {"RR_TOO_LOW", "OVEREXTENDED", "WAIT_CONFIRMATION"}
        entered = row.final_action in {"ENTER_LONG", "ENTER_SHORT"}
        outcome = {
            "direction": direction, "referencePrice": round(entry, 5),
            "riskDistance": round(risk, 5), "mfe_r": round(mfe_r, 3),
            "mae_r": round(mae_r, 3), "net_r": round(net_r, 3),
            "missed_opportunity": missed,
            "missed_long_move": missed and direction == "LONG",
            "missed_short_move": missed and direction == "SHORT",
            "blocked_good_trade": blocked,
            "overly_strict_filter": blocked and mfe_r >= 2,
            "entry_then_stop": entered and mae_r <= -1,
            "fake_breakout": entered and mae_r <= -1 and mfe_r < .5,
            "bad_rr_entry": entered and selected.get("risk_reward") is not None
                            and float(selected["risk_reward"]) < 1.5,
            "overextended_entry": entered and "OVEREXTENDED" in
                                  ((payload.get("finalDecision") or {}).get("secondaryReasons") or []),
            "late_entry": entered and mae_r <= -1 and mfe_r >= 1,
            "evaluatedThrough": _utc(candles[-1].close_time).isoformat(),
            "evaluationVersion": "decision-replay-outcome-v1",
        }
        row.outcome = outcome
        changed += 1
    return changed


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

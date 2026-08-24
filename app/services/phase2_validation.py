"""Phase 2 immutable journals, realistic shadow outcomes and validation gates."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median

from sqlalchemy import select

from app import STRATEGY_BASELINE_VERSION, STRATEGY_FROZEN
from app.db.models import (
    Candle,
    DecisionConflictAudit,
    DecisionJournal,
    HumanOverrideAudit,
    Phase2DailyReport,
    SetupOutcome,
)

MIN_COMPLETE_SETUPS = 50


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _number(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _candidate_snapshot(data: dict, decision: dict, candidate: dict, *, primary: bool) -> dict:
    normalized = data.get("normalized_analysis") or {}
    timeframes = data.get("timeframes") or {}
    zone = list(candidate.get("entry_zone") or [])
    setup_id = str(candidate.get("scenario_id") or "")
    direction = str(candidate.get("direction") or "NONE")
    final = str(decision.get("finalAction") or "WAIT")
    event_risk = data.get("event_risk") or {}
    return {
        "setupId": setup_id, "strategyVersion": STRATEGY_BASELINE_VERSION,
        "strategyFrozen": STRATEGY_FROZEN,
        "createdAt": str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat()),
        "marketRegime4H": str((timeframes.get("h4") or {}).get("trend") or "UNKNOWN"),
        "structure1H": str((timeframes.get("h1") or {}).get("structure") or "UNKNOWN"),
        "setup15M": str(candidate.get("lifecycle_state") or "UNKNOWN"),
        "execution5M": str((data.get("entry_engine") or {}).get("trigger_condition") or "UNKNOWN"),
        "strategyType": str(candidate.get("setup_type") or "OTHER"),
        "direction": direction, "currentPrice": normalized.get("currentPrice"),
        "primaryTrigger": (candidate.get("level_sources") or {}).get("trigger"),
        "triggerConfirmed": candidate.get("lifecycle_state") == "ENTRY_READY",
        "entryZone": zone, "maxChasePrice": candidate.get("chase_limit"),
        "warningLevel": (data.get("trade_plan_manager") or {}).get("warningLevel"),
        "softInvalidation": (candidate.get("level_sources") or {}).get("invalidation"),
        "hardInvalidation": candidate.get("invalidation_price"),
        "targets": list(candidate.get("targets") or []),
        "RR": candidate.get("risk_reward"), "confidence": candidate.get("raw_score"),
        "grade": decision.get("qualityGrade"),
        "whipsawScore": (data.get("regime_state_machine") or {}).get("whipsawScore"),
        "doubleSweepProfile": (data.get("double_sweep_statistical") or {}).get("profile") or {},
        "reasonCodes": [decision.get("primaryReason"), *(decision.get("secondaryReasons") or [])],
        "rawMarketSnapshotId": str(data.get("version") or ""),
        "marketDataTimestamp": normalized.get("marketDataTimestamp"),
        "sourceCandleCloseTime": normalized.get("lastClosedCandleTimestamp"),
        "finalAction": final, "canEnter": bool(decision.get("canEnter")),
        "isPrimary": primary, "session": _session(str(data.get("timestamp_utc") or "")),
        "eventRisk": str(event_risk.get("status") or event_risk.get("eventRisk") or "UNKNOWN"),
        "executionCosts": {
            "spread": (data.get("current_price") or {}).get("spread") or 0,
            "slippageModel": "max(spread*0.25, ATR*0.02)", "fees": 0,
        },
        "features": {
            "htfAligned": ((direction == "LONG" and "BULL" in str(
                (timeframes.get("h4") or {}).get("trend") or "").upper()) or
                (direction == "SHORT" and "BEAR" in str(
                    (timeframes.get("h4") or {}).get("trend") or "").upper())),
            "reclaimQuality": ((data.get("double_sweep_statistical") or {}).get("event") or {}).get("reclaimQuality"),
            "rr": candidate.get("risk_reward"),
            "whipsawScore": (data.get("regime_state_machine") or {}).get("whipsawScore"),
            "entryDistanceAtr": candidate.get("distance_atr"),
        },
    }


def _session(raw: str) -> str:
    try:
        hour = datetime.fromisoformat(raw.replace("Z", "+00:00")).hour
    except ValueError:
        return "UNKNOWN"
    if hour < 7:
        return "ASIA"
    if hour < 12:
        return "LONDON"
    if hour < 16:
        return "LONDON_NY_OVERLAP"
    if hour < 21:
        return "NEW_YORK"
    return "OFF_HOURS"


def persist_decision_journals(db, *, symbol: str, data: dict, decision: dict) -> int:
    """Insert-only: every candidate is tracked; none can be rewritten later."""
    candidates = list(decision.get("signalCandidates") or [])
    selected = str(decision.get("selectedScenarioId") or "")
    created = 0
    for candidate in candidates:
        setup_id = str(candidate.get("scenario_id") or "")
        if not setup_id:
            continue
        if db.execute(select(DecisionJournal.id).where(
                DecisionJournal.setup_id == setup_id,
                DecisionJournal.strategy_version == STRATEGY_BASELINE_VERSION,
        )).scalar_one_or_none() is not None:
            continue
        snapshot = _candidate_snapshot(
            data, decision, candidate, primary=setup_id == selected)
        journal_id = "DJ-" + hashlib.sha256(
            f"{setup_id}|{STRATEGY_BASELINE_VERSION}".encode()).hexdigest()[:24]
        db.add(DecisionJournal(
            journal_id=journal_id, setup_id=setup_id,
            decision_id=str(decision.get("decisionId") or ""),
            strategy_version=STRATEGY_BASELINE_VERSION, symbol=symbol,
            strategy_type=str(snapshot["strategyType"]),
            direction=str(snapshot["direction"]),
            is_primary=bool(snapshot["isPrimary"]), snapshot=snapshot,
            post_analysis={}, created_at=datetime.now(timezone.utc)))
        created += 1
    return created


def backfill_setup_outcomes(db, *, now: datetime, lookback_days: int = 90,
                            limit: int = 5000) -> int:
    """Realistic point-in-time shadow fills; journals remain immutable."""
    oldest = now - timedelta(days=max(2, lookback_days))
    journals = db.execute(select(DecisionJournal).where(
        DecisionJournal.created_at >= oldest).order_by(
            DecisionJournal.created_at.asc()).limit(limit)).scalars().all()
    changed = 0
    for journal in journals:
        snap = dict(journal.snapshot or {})
        direction = journal.direction
        if direction not in {"LONG", "SHORT"}:
            continue
        start = _utc(journal.created_at)
        candles = db.execute(select(Candle).where(
            Candle.symbol == journal.symbol, Candle.timeframe == "15M",
            Candle.is_closed.is_(True), Candle.close_time > start,
            Candle.close_time <= min(now, start + timedelta(days=1)),
        ).order_by(Candle.close_time.asc())).scalars().all()
        candles = [c for c in candles if _utc(c.received_at) >= start]
        if not candles:
            continue
        existing = db.execute(select(SetupOutcome).where(
            SetupOutcome.journal_id == journal.journal_id)).scalar_one_or_none()
        evaluated = _utc(candles[-1].close_time)
        if existing and existing.evaluated_through and _utc(
                existing.evaluated_through) >= evaluated:
            continue
        zone = list(snap.get("entryZone") or [])
        if len(zone) != 2:
            continue
        low, high = sorted(map(float, zone))
        atr = abs(high - low) or max(float(snap.get("currentPrice") or 0) * .001, .01)
        spread = max(float((snap.get("executionCosts") or {}).get("spread") or 0), 0)
        slippage = max(spread * .25, atr * .02)
        trigger = snap.get("primaryTrigger") or {}
        trigger_price = _number(trigger.get("price") if isinstance(trigger, dict) else trigger)
        confirmation_index = 0 if snap.get("triggerConfirmed") else next((
            i for i, candle in enumerate(candles)
            if trigger_price is not None and (
                float(candle.close) >= trigger_price if direction == "LONG"
                else float(candle.close) <= trigger_price)), None)
        entry_triggered = confirmation_index is not None
        fill_index = next((i for i, c in enumerate(candles)
                           if entry_triggered and i >= int(confirmation_index)
                           and float(c.low) <= high and float(c.high) >= low), None)
        entry_captured = entry_triggered and fill_index is not None
        reference = ((low + high) / 2 + slippage if direction == "LONG"
                     else (low + high) / 2 - slippage)
        hard = _number(snap.get("hardInvalidation"))
        if hard is None:
            continue
        risk = abs(reference - hard) + spread
        if risk <= 0:
            continue
        sign = 1 if direction == "LONG" else -1
        active = candles[fill_index:] if fill_index is not None else candles
        favorable = [sign * (float(c.high if sign > 0 else c.low) - reference) for c in active]
        adverse = [sign * (float(c.low if sign > 0 else c.high) - reference) for c in active]
        mfe, mae = max(favorable, default=0), min(adverse, default=0)
        targets = [float(x) for x in snap.get("targets") or [] if _number(x) is not None]
        tp_hits = [any(float(c.high) >= target if sign > 0 else float(c.low) <= target
                       for c in active) for target in targets[:3]]
        hard_index = next((i for i, c in enumerate(active)
                           if (float(c.low) <= hard if sign > 0
                               else float(c.high) >= hard)), None)
        hard_hit = hard_index is not None
        end_move = sign * (float(active[-1].close) - reference) - spread
        realized_r = (-1.0 if hard_hit else end_move / risk)
        success = bool(tp_hits and tp_hits[0])
        false_stop = hard_hit and any(
            float(c.high) >= reference if sign > 0 else float(c.low) <= reference
            for c in active[int(hard_index) + 1:])
        mfe_index = favorable.index(mfe) if favorable else None
        mae_index = adverse.index(mae) if adverse else None
        tp_indexes = [next((i for i, c in enumerate(active)
                            if (float(c.high) >= target if sign > 0
                                else float(c.low) <= target)), None)
                      for target in targets[:3]]
        max_chase = _number(snap.get("maxChasePrice"))
        missed_valid = bool(entry_triggered and not snap.get("canEnter") and max_chase is not None
                            and ((direction == "LONG" and low <= max_chase)
                                 or (direction == "SHORT" and high >= max_chase)))
        outcome = {
            "directionCorrect": mfe >= risk, "entryTriggered": entry_triggered,
            "entryCaptured": entry_captured, "entryMissed": not entry_captured,
            "missedValidEntry": missed_valid,
            "TP1Hit": tp_hits[0] if tp_hits else False,
            "TP2Hit": tp_hits[1] if len(tp_hits) > 1 else False,
            "TP3Hit": tp_hits[2] if len(tp_hits) > 2 else False,
            "warningTriggered": abs(mae) >= risk * .35,
            "softInvalidationTriggered": hard_hit,
            "hardInvalidationTriggered": hard_hit,
            "MFE": round(mfe, 5), "MAE": round(abs(mae), 5),
            "mfeR": round(mfe / risk, 4), "maeR": round(abs(mae) / risk, 4),
            "realizedR": round(realized_r, 4),
            "maximumPossibleR": round(mfe / risk, 4),
            "timeToMFEMinutes": mfe_index * 15 if mfe_index is not None else None,
            "timeToMAEMinutes": mae_index * 15 if mae_index is not None else None,
            "timeToTPMinutes": [index * 15 if index is not None else None
                                for index in tp_indexes],
            "timeToInvalidationMinutes": hard_index * 15 if hard_index is not None else None,
            "holdingTimeMinutes": max(0, (len(active) - 1) * 15),
            "potentialFalseStop": false_stop, "success": success,
            "fillPrice": round(reference, 5), "spread": spread,
            "slippage": round(slippage, 5), "evaluationVersion": "phase2-outcome-v1",
        }
        row = existing or SetupOutcome(
            journal_id=journal.journal_id, created_at=now, updated_at=now)
        if existing is None:
            db.add(row)
        row.status = "COMPLETE" if evaluated >= start + timedelta(hours=4) else "ACTIVE"
        row.outcome, row.evaluated_through, row.updated_at = outcome, evaluated, now
        changed += 1
    return changed


def _sample_gate(n: int) -> str:
    return "INSUFFICIENT" if n < 20 else "EXPERIMENTAL" if n < 50 else (
        "MODERATE" if n < 100 else "STRONGER")


def _metrics(rows: list[tuple[DecisionJournal, SetupOutcome]]) -> dict:
    settled = [(j, o) for j, o in rows if o.status == "COMPLETE"]
    values = [float((o.outcome or {}).get("realizedR") or 0) for _, o in settled]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    equity = drawdown = peak = 0.0
    for value in values:
        equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
    n = len(settled)
    return {
        "sampleSize": n, "sampleGate": _sample_gate(n),
        "winRate": round(len(wins) / n, 4) if n else None,
        "averageR": round(mean(values), 4) if values else None,
        "medianR": round(median(values), 4) if values else None,
        "expectancy": round(mean(values), 4) if values else None,
        "profitFactor": round(sum(wins) / abs(sum(losses)), 4) if losses else None,
        "maxDrawdownR": round(drawdown, 4),
        "directionAccuracy": round(sum(bool(o.outcome.get("directionCorrect"))
                                       for _, o in settled) / n, 4) if n else None,
        "entryCaptureRate": round(sum(bool(o.outcome.get("entryCaptured"))
                                      for _, o in settled) / n, 4) if n else None,
        "missedEntryRate": round(sum(bool(o.outcome.get("missedValidEntry"))
                                     for _, o in settled) / n, 4) if n else None,
        "falseStopRate": round(sum(bool(o.outcome.get("potentialFalseStop"))
                                   for _, o in settled) / n, 4) if n else None,
        "averageMAE": round(mean([float(o.outcome.get("maeR") or 0)
                                  for _, o in settled]), 4) if n else None,
        "averageMFE": round(mean([float(o.outcome.get("mfeR") or 0)
                                  for _, o in settled]), 4) if n else None,
        "mfeMaeRatio": round(
            mean([float(o.outcome.get("mfeR") or 0) for _, o in settled]) /
            max(mean([float(o.outcome.get("maeR") or 0) for _, o in settled]), .0001), 4)
        if n else None,
        "trueInvalidationRate": round(sum(bool(o.outcome.get(
            "hardInvalidationTriggered")) and not bool(o.outcome.get("potentialFalseStop"))
            for _, o in settled) / n, 4) if n else None,
        "averageHoldingTimeMinutes": round(mean([float(o.outcome.get(
            "holdingTimeMinutes") or 0) for _, o in settled]), 1) if n else None,
    }


def _calibration(rows: list[tuple[DecisionJournal, SetupOutcome]]) -> dict:
    settled = [(j, o) for j, o in rows if o.status == "COMPLETE"
               and _number(j.snapshot.get("confidence")) is not None]
    buckets, brier = defaultdict(list), []
    for journal, outcome in settled:
        score = float(journal.snapshot["confidence"])
        key = "90+" if score >= 90 else f"{int(score // 10) * 10}-{int(score // 10) * 10 + 9}"
        success = 1.0 if outcome.outcome.get("success") else 0.0
        buckets[key].append((score / 100, success, float(outcome.outcome.get("realizedR") or 0)))
        brier.append((score / 100 - success) ** 2)
    ece, details, total = 0.0, [], len(settled)
    for key, values in sorted(buckets.items()):
        predicted, actual = mean([x[0] for x in values]), mean([x[1] for x in values])
        ece += len(values) / max(total, 1) * abs(predicted - actual)
        details.append({"bucket": key, "sampleSize": len(values),
                        "meanConfidence": round(predicted, 4),
                        "winRate": round(actual, 4),
                        "averageR": round(mean([x[2] for x in values]), 4),
                        "status": "OVERCONFIDENT" if predicted - actual > .1 else
                                  "UNDERCONFIDENT" if actual - predicted > .1 else "CALIBRATED"})
    return {"sampleSize": total, "ECE": round(ece, 4) if total else None,
            "brierScore": round(mean(brier), 4) if brier else None,
            "buckets": details, "label": "SETUP CONFIDENCE SCORE",
            "historicalProbabilityAllowed": total >= 100 and ece <= .1}


def _feature_importance(rows: list[tuple[DecisionJournal, SetupOutcome]]) -> list[dict]:
    """Transparent univariate OOS diagnostic, not an auto-tuning model."""
    output = []
    feature_names = sorted({key for journal, _ in rows
                            for key in (journal.snapshot.get("features") or {})})
    for name in feature_names:
        observed = []
        for journal, outcome in rows:
            value = (journal.snapshot.get("features") or {}).get(name)
            if isinstance(value, (bool, int, float)) and outcome.status == "COMPLETE":
                observed.append((float(value), float(outcome.outcome.get("realizedR") or 0)))
        if len(observed) < 20:
            output.append({"feature": name, "sampleSize": len(observed),
                           "strength": "INSUFFICIENT"})
            continue
        observed.sort()
        cut = len(observed) // 2
        lift = mean([x[1] for x in observed[cut:]]) - mean([x[1] for x in observed[:cut]])
        strength = "STRONG" if abs(lift) >= .5 else "MODERATE" if abs(lift) >= .2 else (
            "WEAK" if abs(lift) >= .05 else "NOISE")
        output.append({"feature": name, "sampleSize": len(observed),
                       "expectancyLiftR": round(lift, 4), "strength": strength})
    return output


def validation_report(db, *, limit: int = 5000) -> dict:
    rows = db.execute(select(DecisionJournal, SetupOutcome).join(
        SetupOutcome, SetupOutcome.journal_id == DecisionJournal.journal_id
    ).order_by(DecisionJournal.created_at.asc()).limit(limit)).all()
    groups = {}
    for name, key_fn in {
        "strategy": lambda j: j.strategy_type,
        "regime": lambda j: str(j.snapshot.get("marketRegime4H") or "UNKNOWN"),
        "session": lambda j: str(j.snapshot.get("session") or "UNKNOWN"),
        "confidence": lambda j: str(int(float(j.snapshot.get("confidence") or 0) // 10) * 10),
        "grade": lambda j: str(j.snapshot.get("grade") or "UNRATED"),
        "eventRisk": lambda j: str(j.snapshot.get("eventRisk") or "UNKNOWN"),
        "hourUtc": lambda j: str(_utc(j.created_at).hour),
    }.items():
        segmented = defaultdict(list)
        for journal, outcome in rows:
            segmented[key_fn(journal)].append((journal, outcome))
        groups[name] = {key: _metrics(value) for key, value in segmented.items()}
    n = len(rows); train_end, validation_end = int(n * .6), int(n * .8)
    splits = {"research": rows[:train_end], "validation": rows[train_end:validation_end],
              "outOfSample": rows[validation_end:]}
    split_metrics = {key: _metrics(value) for key, value in splits.items()}
    oos = split_metrics["outOfSample"]
    walk_forward_passed = n >= 100 and all(
        _metrics(rows[start:start + 20]).get("expectancy", 0) > 0
        for start in range(40, n - 19, 20))
    calibration = _calibration(rows)
    a_plus = [{"strategy": key, **value} for key, value in groups["strategy"].items()
              if value["sampleSize"] >= 100 and (value["expectancy"] or 0) > 0
              and (value["maxDrawdownR"] or 999) <= 10
              and (oos.get("expectancy") or 0) > 0 and walk_forward_passed]
    grade_order = [groups["grade"].get(name) for name in ("C", "B", "A", "A+")]
    grade_ready = all(item and item["sampleSize"] >= 20 for item in grade_order)
    grade_monotonic = bool(grade_ready and all(
        float(grade_order[index]["expectancy"]) < float(grade_order[index + 1]["expectancy"])
        for index in range(len(grade_order) - 1)))
    overall = _metrics(rows)
    phase_passed = bool(
        overall["sampleSize"] >= MIN_COMPLETE_SETUPS
        and (oos.get("sampleSize") or 0) >= 20
        and (oos.get("expectancy") or 0) > 0
        and walk_forward_passed
        and calibration["historicalProbabilityAllowed"])
    rolling = {str(size): _metrics(rows[-size:]) for size in (20, 50, 100)}
    current = datetime.now(timezone.utc)
    recency = {
        "3m": _metrics([(j, o) for j, o in rows if _utc(j.created_at) >= current - timedelta(days=90)]),
        "6m": _metrics([(j, o) for j, o in rows if _utc(j.created_at) >= current - timedelta(days=180)]),
        "12m": _metrics([(j, o) for j, o in rows if _utc(j.created_at) >= current - timedelta(days=365)]),
        "all": overall,
    }
    overrides = db.execute(select(HumanOverrideAudit)).scalars().all()
    today = current.date()
    todays_rows = [(j, o) for j, o in rows if _utc(j.created_at).date() == today]
    conflicts_today = db.execute(select(DecisionConflictAudit).where(
        DecisionConflictAudit.created_at >= datetime.combine(
            today, datetime.min.time(), tzinfo=timezone.utc))).scalars().all()
    feature_importance = _feature_importance(splits["outOfSample"])
    anti_overfit_passed = bool(
        (split_metrics["validation"].get("expectancy") or 0) > 0
        and (oos.get("expectancy") or 0) > 0 and walk_forward_passed)
    return {
        "strategyBaselineVersion": STRATEGY_BASELINE_VERSION,
        "strategyFrozen": STRATEGY_FROZEN, "overall": overall,
        "groups": groups, "splits": split_metrics,
        "walkForward": {"passed": walk_forward_passed,
                        "reason": "需要至少100筆且每個向前20筆窗口 expectancy > 0"},
        "calibration": calibration, "aPlusSetups": a_plus,
        "rolling": rolling,
        "recency": recency, "edgeDecay": bool(
            (overall.get("expectancy") or 0) > 0
            and recency["3m"]["sampleSize"] >= 20
            and (recency["3m"].get("expectancy") or 0) <= 0),
        "featureImportance": feature_importance,
        "gradeValidation": {"sampleReady": grade_ready,
                            "monotonicCtoAPlus": grade_monotonic,
                            "validated": grade_ready and grade_monotonic},
        "evidenceTable": [{
            "setupId": journal.setup_id, "role": "PRIMARY ACTIVE SETUP"
            if journal.is_primary else "SECONDARY CANDIDATE",
            "strategyType": journal.strategy_type,
            "grade": journal.snapshot.get("grade"),
            "confidenceLabel": "SETUP CONFIDENCE SCORE",
            "confidence": journal.snapshot.get("confidence"),
            "positiveFeatures": [key for key, value in (
                journal.snapshot.get("features") or {}).items() if value is True],
            "negativeFeatures": [key for key, value in (
                journal.snapshot.get("features") or {}).items() if value is False],
            "sameTypeSample": groups["strategy"].get(journal.strategy_type, {}).get(
                "sampleSize", 0),
            "sameTypeWinRate": groups["strategy"].get(journal.strategy_type, {}).get(
                "winRate"),
            "sameTypeAverageR": groups["strategy"].get(journal.strategy_type, {}).get(
                "averageR"),
        } for journal, _ in rows[-50:]],
        "antiOverfittingGate": {"passed": anti_overfit_passed,
                                "autoDeployAllowed": False},
        "complexityPolicy": "表現相近時優先較少條件；任何移除需通過 OOS",
        "humanOverride": {"sampleSize": len(overrides), "status": "SEPARATE_LEDGER"},
        "dailyReview": {
            "dateUtc": today.isoformat(), "todaysSetups": len(todays_rows),
            "correctDirections": sum(bool(o.outcome.get("directionCorrect"))
                                     for _, o in todays_rows),
            "wrongDirections": sum(not bool(o.outcome.get("directionCorrect"))
                                   for _, o in todays_rows),
            "validEntries": sum(bool(o.outcome.get("entryCaptured"))
                                for _, o in todays_rows),
            "missedEntries": sum(bool(o.outcome.get("missedValidEntry"))
                                 for _, o in todays_rows),
            "falseStops": sum(bool(o.outcome.get("potentialFalseStop"))
                              for _, o in todays_rows),
            "systemBugs": len(conflicts_today),
            "humanOverrides": sum(_utc(item.created_at).date() == today
                                  for item in overrides),
        },
        "phase2ValidationPassed": phase_passed,
        "status": "PHASE 2 VALIDATION PASSED" if phase_passed else
                  "COLLECTING_OR_NOT_VALIDATED",
        "answers": {
            "trueAccuracy": overall["directionAccuracy"],
            "bestSetups": [item["strategy"] for item in a_plus],
            "avoidSetups": [key for key, value in groups["strategy"].items()
                            if value["sampleSize"] >= 50 and (value["expectancy"] or 0) <= 0],
        },
    }


def persist_daily_validation_report(db, *, now: datetime) -> dict:
    """Upsert the current UTC-day review; setup journals remain immutable."""
    report = validation_report(db)
    payload = dict(report.get("dailyReview") or {})
    row = db.execute(select(Phase2DailyReport).where(
        Phase2DailyReport.report_date == now.date())).scalar_one_or_none()
    if row is None:
        row = Phase2DailyReport(
            report_date=now.date(), strategy_version=STRATEGY_BASELINE_VERSION,
            payload=payload, updated_at=now)
        db.add(row)
    else:
        row.payload, row.updated_at = payload, now
    return payload


def record_human_override(db, *, journal_id: str, override_type: str,
                          payload: dict, now: datetime | None = None) -> None:
    """Never merge discretionary execution into deterministic system outcomes."""
    allowed = {"EARLY_ENTRY", "EARLY_EXIT", "DELAY_STOP", "ADD_POSITION",
               "CANCEL_STOP", "MANUAL_RISK_OVERRIDE"}
    if override_type not in allowed:
        raise ValueError("未知的 HUMAN_OVERRIDE 類型")
    db.add(HumanOverrideAudit(
        journal_id=journal_id, override_type=override_type,
        payload=dict(payload), created_at=now or datetime.now(timezone.utc)))

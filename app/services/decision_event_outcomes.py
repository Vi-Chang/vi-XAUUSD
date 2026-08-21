"""Forward-only performance loop for canonical decision events."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Candle, DecisionEvent, DecisionEventOutcome

HORIZONS = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1),
            "4h": timedelta(hours=4), "1d": timedelta(days=1)}
EXECUTABLE = {"LONG_READY": "LONG", "SHORT_READY": "SHORT"}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cost(event: DecisionEvent) -> float:
    payload = event.payload or {}
    costs = payload.get("executionCosts") or {}
    spread = float(costs.get("spread") or payload.get("spread") or 0)
    slippage = float(costs.get("slippage") or 0)
    fees = float(costs.get("fees") or 0)
    return max(0.0, spread + slippage + fees)


def backfill_decision_event_outcomes(db, *, now: datetime,
                                     lookback_days: int = 30,
                                     limit: int = 5000) -> int:
    """Evaluate only candles received after each event was created."""
    oldest = now - timedelta(days=max(2, lookback_days))
    events = db.execute(select(DecisionEvent).where(
        DecisionEvent.created_at >= oldest,
        DecisionEvent.current_state.in_(tuple(EXECUTABLE)),
    ).order_by(DecisionEvent.created_at.asc()).limit(limit)).scalars().all()
    changed = 0
    for event in events:
        start = _utc(event.created_at)
        candles = db.execute(select(Candle).where(
            Candle.symbol == event.symbol, Candle.timeframe == "15M",
            Candle.is_closed.is_(True), Candle.close_time > start,
            Candle.close_time <= now,
        ).order_by(Candle.close_time.asc())).scalars().all()
        candles = [c for c in candles if _utc(c.received_at) >= start]
        if not candles:
            continue
        existing = db.execute(select(DecisionEventOutcome).where(
            DecisionEventOutcome.event_id == event.event_id)).scalar_one_or_none()
        if (existing is not None and existing.evaluated_through is not None
                and _utc(existing.evaluated_through) >= _utc(candles[-1].close_time)):
            continue
        direction = EXECUTABLE[event.current_state]
        sign = 1 if direction == "LONG" else -1
        entry = float(event.current_price)
        risk = abs(entry - float(event.stop_loss)) if event.stop_loss is not None else None
        cost = round(_cost(event), 6)
        favorable = max(sign * (float(c.high if sign > 0 else c.low) - entry) for c in candles) - cost
        adverse = min(sign * (float(c.low if sign > 0 else c.high) - entry) for c in candles) - cost
        targets = [float(v) for v in (event.targets or []) if isinstance(v, (int, float))]
        tp1_hit = bool(targets and any(
            c.high >= targets[0] if sign > 0 else c.low <= targets[0] for c in candles))
        stop_hit = bool(event.stop_loss is not None and any(
            c.low <= event.stop_loss if sign > 0 else c.high >= event.stop_loss for c in candles))
        horizon_values = {}
        for name, delta in HORIZONS.items():
            settled = next((c for c in candles if _utc(c.close_time) >= start + delta), None)
            if settled:
                net_move = sign * (float(settled.close) - entry) - cost
                horizon_values[name] = {
                    "net_move": round(net_move, 3),
                    "net_r": round(net_move / risk, 3) if risk else None,
                    "candle_close_time": _utc(settled.close_time).isoformat(),
                }
        row = existing
        if row is None:
            row = DecisionEventOutcome(event_id=event.event_id, direction=direction,
                entry_price=entry, created_at=now, updated_at=now)
            db.add(row)
        row.initial_risk, row.transaction_cost = risk, cost
        assistant = (event.payload or {}).get("decisionAssistant") or {}
        row.setup_type = str(assistant.get("scenarioType") or "OTHER")
        row.market_regime = str(assistant.get("regime") or "NO_EDGE")
        row.entry_quality_score = assistant.get("entryQualityScore")
        row.horizons = horizon_values
        row.tp1_hit, row.stop_hit = tp1_hit, stop_hit
        row.max_favorable_r = round(favorable / risk, 3) if risk else None
        row.max_adverse_r = round(adverse / risk, 3) if risk else None
        row.classification = ("TP1_REACHED" if tp1_hit else "STOPPED" if stop_hit
                              else "ACTIVE" if "1d" not in horizon_values else "EXPIRED")
        row.evaluated_through = _utc(candles[-1].close_time)
        row.updated_at = now
        changed += 1
    return changed


def decision_event_performance(db, *, limit: int = 5000) -> dict:
    rows = db.execute(select(DecisionEventOutcome)
                      .order_by(DecisionEventOutcome.created_at.desc())
                      .limit(limit)).scalars().all()
    settled = [r for r in rows if "1h" in (r.horizons or {})]
    wins = [r for r in settled if (r.horizons["1h"].get("net_r") or 0) > 0]
    by_direction = {}
    for direction in ("LONG", "SHORT"):
        group = [r for r in settled if r.direction == direction]
        by_direction[direction] = {
            "sample_size": len(group),
            "win_rate_pct": round(100 * sum((r.horizons["1h"].get("net_r") or 0) > 0
                                             for r in group) / len(group), 1) if group else None,
        }
    events = {event.event_id: event for event in db.execute(
        select(DecisionEvent).where(DecisionEvent.event_id.in_([r.event_id for r in settled]))
    ).scalars().all()} if settled else {}
    by_setup: dict[str, dict] = {}
    for row in settled:
        event = events.get(row.event_id)
        payload = event.payload if event else {}
        setup = (payload or {}).get("setup") or {}
        setup_type = str(row.setup_type or setup.get("type") or payload.get("setupType") or "OTHER")
        bucket = by_setup.setdefault(setup_type, {"sample_size": 0, "net_r": [], "wins": 0,
                                                  "mfe": [], "mae": [], "false_breaks": 0})
        net_r = float((row.horizons.get("1h") or {}).get("net_r") or 0)
        bucket["sample_size"] += 1
        bucket["net_r"].append(net_r)
        bucket["wins"] += int(net_r > 0)
        if row.max_favorable_r is not None:
            bucket["mfe"].append(float(row.max_favorable_r))
        if row.max_adverse_r is not None:
            bucket["mae"].append(float(row.max_adverse_r))
        bucket["false_breaks"] += int(row.classification == "STOPPED" and net_r < 0)
    setup_report = {name: {
        "sample_size": values["sample_size"],
        "win_rate_pct": round(100 * values["wins"] / values["sample_size"], 1),
        "average_net_r": round(sum(values["net_r"]) / values["sample_size"], 3),
        "expectancy_r": round(sum(values["net_r"]) / values["sample_size"], 3),
        "average_mfe_r": round(sum(values["mfe"]) / len(values["mfe"]), 3) if values["mfe"] else None,
        "average_mae_r": round(sum(values["mae"]) / len(values["mae"]), 3) if values["mae"] else None,
        "false_breakout_rate_pct": round(100 * values["false_breaks"] / values["sample_size"], 1),
        "calibration_ready": values["sample_size"] >= 30,
    } for name, values in by_setup.items()}
    return {"sample_size": len(rows), "settled_1h": len(settled),
            "win_rate_1h_pct": round(100 * len(wins) / len(settled), 1) if settled else None,
            "by_direction": by_direction,
            "by_setup": setup_report,
            "calibration_ready": len(settled) >= 30,
            "note": "績效已扣除事件記錄中的點差、滑價與費用；此數值不是未來勝率。"}

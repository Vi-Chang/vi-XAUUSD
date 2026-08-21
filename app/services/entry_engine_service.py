"""Persist, expose and notify the executable entry lifecycle."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select

from app.db.models import EntrySetupState
from app.db.session import db_session
from app.engines.entry_engine import EntryPlan, evaluate_entry_engine


def _load(symbol: str) -> EntryPlan:
    with db_session() as db:
        row = db.execute(select(EntrySetupState).where(
            EntrySetupState.symbol == symbol)).scalar_one_or_none()
        if row is None or not row.plan:
            return EntryPlan()
        raw = dict(row.plan)
        raw["notified_states"] = tuple(raw.get("notified_states") or ())
        return EntryPlan(**raw)


def _save(symbol: str, plan: EntryPlan) -> None:
    raw = asdict(plan)
    raw["notified_states"] = list(plan.notified_states)
    with db_session() as db:
        row = db.execute(select(EntrySetupState).where(
            EntrySetupState.symbol == symbol)).scalar_one_or_none()
        if row is None:
            row = EntrySetupState(symbol=symbol, updated_at=datetime.now(timezone.utc))
            db.add(row)
        row.setup_id, row.status, row.plan = plan.setup_id, plan.status, raw
        row.updated_at = datetime.now(timezone.utc)


def evaluate_and_persist_entry(data: dict, *, m5_closed: pd.DataFrame | None,
                               m15_closed: pd.DataFrame | None,
                               now: datetime) -> EntryPlan:
    symbol = str(data.get("symbol") or "XAUUSD")
    previous = _load(symbol)
    evaluation = evaluate_entry_engine(data, previous, m5_closed=m5_closed,
                                       m15_closed=m15_closed, now=now)
    if evaluation.plan != previous:
        _save(symbol, evaluation.plan)
    return evaluation.plan


async def notify_entry_plan(plan_data: dict, notifier, *, symbol: str = "XAUUSD") -> None:
    """Deprecated compatibility hook: entry engines never push directly.

    The persisted plan is collected as a SignalCandidate during the next
    FinalDecisionEngine evaluation; only its canonical outbox event may notify.
    """
    return

"""Sequential entry-engine replay; each step sees only data available at that time."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from app.engines.entry_engine import EntryPlan, evaluate_entry_engine


@dataclass(frozen=True)
class ReplayStep:
    at: datetime
    analysis: dict
    m5_closed: pd.DataFrame | None = None
    m15_closed: pd.DataFrame | None = None


@dataclass(frozen=True)
class ReplayTransition:
    at: datetime
    setup_id: str
    old_status: str
    new_status: str
    direction: str
    entry: float | None
    reason: str


def replay_entry_engine(steps: Iterable[ReplayStep]) -> list[ReplayTransition]:
    """Replay chronologically and record state changes without future candles."""
    previous = EntryPlan()
    transitions: list[ReplayTransition] = []
    last_at: datetime | None = None
    for step in steps:
        if last_at is not None and step.at < last_at:
            raise ValueError("replay steps must be chronological")
        evaluation = evaluate_entry_engine(
            step.analysis, previous, m5_closed=step.m5_closed,
            m15_closed=step.m15_closed, now=step.at,
        )
        plan = evaluation.plan
        if plan.status != previous.status or plan.setup_id != previous.setup_id:
            transitions.append(ReplayTransition(
                at=step.at, setup_id=plan.setup_id, old_status=previous.status,
                new_status=plan.status, direction=plan.direction,
                entry=plan.suggested_entry,
                reason=plan.trigger_condition or plan.missing_condition,
            ))
        previous, last_at = plan, step.at
    return transitions

"""Grouped deterministic evidence scoring.

Signals from the same family are capped so several correlated structure labels
cannot inflate confidence.  The directional margin also matters: a setup with
nearly equal opposing evidence receives a conflict penalty.
"""
from __future__ import annotations

WEIGHTS = {"STRUCT": 15, "LEVEL": 15, "MOMO": 15, "HTF": 10}
CAPS = {"STRUCT": 40, "LEVEL": 20, "MOMO": 15, "HTF": 20}


def _category(condition: str) -> str:
    return condition.split(":", 1)[0]


def grouped_evidence_score(dominant: list[str], opposing: list[str], *,
                           quality_good: bool, chase: bool) -> int:
    grouped: dict[str, int] = {}
    for condition in set(dominant):
        category = _category(condition)
        if category not in WEIGHTS:
            continue
        grouped[category] = min(CAPS[category], grouped.get(category, 0) + WEIGHTS[category])

    score = sum(grouped.values())
    if quality_good:
        score += 10
    if chase:
        score -= 15

    margin = len(set(dominant)) - len(set(opposing))
    if margin <= 0:
        score -= 25
    elif margin == 1:
        score -= 10
    return max(0, min(100, score))

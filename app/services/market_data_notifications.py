"""Compatibility shim for the retired direct market-data notifier.

Data-health transitions are persisted by the canonical DecisionEvent outbox.
Keeping a second sender here previously allowed the scheduler and outbox to
emit contradictory or duplicate Telegram messages.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def notify_market_data_transition(*, notifier, previous: str | None,
                                        health: dict, payload: dict) -> str:
    """Track compatibility state without sending a Telegram message."""
    del notifier, payload
    current = str(health.get("status") or "GOOD")
    if current != previous:
        logger.info(
            "direct market-data notification suppressed; canonical outbox owns %s -> %s",
            previous, current,
        )
    return current

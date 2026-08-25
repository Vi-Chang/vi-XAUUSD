"""Choose one user-facing market notification for each decision snapshot."""
from __future__ import annotations

from app.services.alert_aggregator import aggregate_signal_facts


def coordinate_notification_intents(symbol: str, events: list[dict]) -> list[dict]:
    """Collapse every intent from one snapshot into one canonical notification.

    Rule engines remain free to emit auditable facts.  Only this coordinator
    decides which fact becomes the user-facing Telegram notification.
    """
    market, log_only = [], []
    for event in events:
        item = dict(event)
        if str(item.get("event_type") or "") in {
                "DELIVERY_UNKNOWN", "OPPORTUNITY_COVERAGE_GAP"}:
            item["notificationEligible"] = False
            item["notificationRoute"] = "LOG_ONLY"
            log_only.append(item)
        else:
            market.append(item)
    coordinated = aggregate_signal_facts(symbol, market)
    for event in coordinated:
        event["notificationCoordinator"] = "single-snapshot-v1"
    # Log-only intents are returned for audit persistence, but notification
    # policy will never enqueue them for Telegram.
    return coordinated + log_only

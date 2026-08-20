import pytest

from app.engines.short_alert_state import ShortAlertState
from app.services import short_alert_service
from tests.test_short_alert_state import payload, short_entry


class Notifier:
    def __init__(self):
        self.events = []

    async def notify(self, level, topic, message, **kwargs):
        self.events.append((level, topic, message, kwargs))
        return True


@pytest.mark.asyncio
async def test_every_transition_pushes_once_and_long_invalidation_does_not_block(monkeypatch):
    stored = {"state": ShortAlertState()}
    monkeypatch.setattr(short_alert_service, "_load", lambda _symbol: stored["state"])
    monkeypatch.setattr(short_alert_service, "_save", lambda _symbol, state: stored.update(state=state))
    notifier = Notifier()
    result = {"symbol": "XAUUSD", "normalized_analysis": payload("confirmed_breakdown")}

    await short_alert_service.process_short_alert(
        result, notifier, entry_plan={"status": "INVALIDATED", "direction": "LONG"})
    await short_alert_service.process_short_alert(
        result, notifier, entry_plan={"status": "INVALIDATED", "direction": "LONG"})
    result["normalized_analysis"] = payload(
        "retest_rejected", closed="2026-08-20T01:15:00+00:00", closed_price=4487)
    await short_alert_service.process_short_alert(result, notifier)
    result["normalized_analysis"] = payload(
        "retest_rejected", closed="2026-08-20T01:30:00+00:00", closed_price=4483)
    await short_alert_service.process_short_alert(result, notifier, entry_plan=short_entry())

    assert [event[1].split(":")[1] for event in notifier.events] == [
        "BREAKDOWN_CONFIRMED", "RETEST_REJECTED", "SHORT_ENTRY_READY"]
    assert all(event[3]["exact_once"] for event in notifier.events)
    assert result["directional_alert"]["status"] == "SHORT_ENTRY_READY"

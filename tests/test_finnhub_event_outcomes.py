import json
from datetime import datetime, timezone

from app.services import event_service as es


def test_finnhub_result_enriches_matching_cpi_event(monkeypatch):
    now = datetime(2026, 8, 13, 13, tzinfo=timezone.utc)
    event = {"name": "Consumer Price Index", "time_utc": "2026-08-13T12:30:00Z"}
    monkeypatch.setattr(es.get_settings(), "finnhub_api_key", "test-key")
    captured: list[str] = []

    def fetch(url: str) -> str:
        captured.append(url)
        return json.dumps({"economicCalendar": [{
            "event": "US Consumer Price Index", "time": "2026-08-13T12:30:00Z",
            "actual": "3.2", "estimate": "3.0", "prev": "2.9",
        }]})

    enriched = es.enrich_event_outcome(event, now, fetcher=fetch)
    assert "token=test-key" in captured[0]
    assert enriched["actual"] == 3.2
    assert enriched["forecast"] == 3.0
    assert enriched["previous"] == 2.9
    assert enriched["outcome_source"] == "Finnhub economic calendar"


def test_finnhub_does_not_apply_a_same_day_different_event(monkeypatch):
    now = datetime(2026, 8, 13, 13, tzinfo=timezone.utc)
    event = {"name": "Consumer Price Index", "time_utc": "2026-08-13T12:30:00Z"}
    monkeypatch.setattr(es.get_settings(), "finnhub_api_key", "test-key")

    def fetch(_url: str) -> str:
        return json.dumps({"economicCalendar": [{
            "event": "US Producer Price Index", "time": "2026-08-13T12:30:00Z",
            "actual": "2.2", "estimate": "2.0", "prev": "1.9",
        }]})

    assert es.enrich_event_outcome(event, now, fetcher=fetch) == event


def test_finnhub_failure_keeps_schedule_record_unchanged(monkeypatch):
    now = datetime(2026, 8, 13, 13, tzinfo=timezone.utc)
    event = {"name": "Consumer Price Index", "time_utc": "2026-08-13T12:30:00Z"}
    monkeypatch.setattr(es.get_settings(), "finnhub_api_key", "test-key")

    def fetch(_url: str) -> str:
        raise OSError("provider unavailable")

    assert es.enrich_event_outcome(event, now, fetcher=fetch) == event

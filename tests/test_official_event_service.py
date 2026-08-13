from datetime import datetime, timedelta, timezone

from app.services import event_service as es


def test_parse_bls_ics_keeps_only_high_impact_releases():
    ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260812T123000Z
SUMMARY:Consumer Price Index
END:VEVENT
BEGIN:VEVENT
DTSTART:20260813T123000Z
SUMMARY:Import and Export Price Indexes
END:VEVENT
END:VCALENDAR
"""
    events = es.parse_bls_ics(ics)
    assert len(events) == 1
    assert events[0]["impact"] == "HIGH"
    assert events[0]["time_utc"] == "2026-08-12T12:30:00Z"


def test_post_release_window_locks_new_entries(monkeypatch):
    now = datetime(2026, 8, 13, 12, 40, tzinfo=timezone.utc)
    event = {"name": "Consumer Price Index", "country": "US", "impact": "HIGH",
             "time_utc": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")}
    monkeypatch.setattr(es, "load_official_events", lambda _: ([event], False, now.isoformat()))
    monkeypatch.setattr(es, "load_manual_events", lambda: ([], True))
    state = es.evaluate_event_risk(now)
    assert state.event_lockout is True
    assert state.post_event_wait is True
    assert state.event_phase == "post_release"


def test_stale_event_data_never_claims_low_event_risk(monkeypatch):
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(es, "load_official_events", lambda _: ([], True, ""))
    monkeypatch.setattr(es, "load_manual_events", lambda: ([], True))
    state = es.evaluate_event_risk(now)
    assert state.level == "UNKNOWN"
    assert state.data_stale is True

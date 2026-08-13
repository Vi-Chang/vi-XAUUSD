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


def test_parse_fomc_calendar_uses_eastern_release_time():
    page = "2026 FOMC Meetings January 27-28 March 17-18* 2025 FOMC Meetings"
    events = es.parse_fomc_calendar(page, 2026)
    assert [event["name"] for event in events] == ["FOMC Rate Decision", "FOMC Rate Decision"]
    # 冬季為 14:00 ET = 19:00 UTC；三月已轉夏令時間，為 18:00 UTC。
    assert events[0]["time_utc"] == "2026-01-28T19:00:00Z"
    assert events[1]["time_utc"] == "2026-03-18T18:00:00Z"


def test_parse_bea_schedule_keeps_headline_gdp_and_pce():
    page = """Year 2026 | July 30 8:30 AM | News | GDP (Advance Estimate), 2nd Quarter 2026 |
    July 30 8:30 AM | News | Personal Income and Outlays, June 2026 |
    August 4 8:30 AM | News | U.S. International Trade in Goods and Services |"""
    events = es.parse_bea_schedule(page, 2026)
    assert [event["name"] for event in events] == ["GDP", "Personal Income and Outlays (PCE)"]
    # 夏令時間的 08:30 ET 應為 12:30 UTC。
    assert all(event["time_utc"] == "2026-07-30T12:30:00Z" for event in events)


def test_one_official_source_failure_does_not_discard_other_events(monkeypatch, tmp_path):
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(es.get_settings(), "official_events_cache_path", str(tmp_path / "events.json"))
    monkeypatch.setattr(es.get_settings(), "app_env", "production")

    def fetch(url):
        if "bls.gov" in url:
            return "BEGIN:VEVENT\nDTSTART:20260814T123000Z\nSUMMARY:Consumer Price Index\nEND:VEVENT"
        raise OSError("source unavailable")

    events, stale, _ = es.load_official_events(now, fetcher=fetch)
    assert stale is False
    assert [event["source"] for event in events] == ["BLS"]


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

from app.services.thesis_risk_research import aggregate_comparison, compare_case


def today_case():
    return {
        "case_id": "2026-08-24-sweep-reclaim-long", "setup_id": "today",
        "direction": "LONG", "strategy_type": "SWEEP_RECLAIM_LONG",
        "suggested_entry": 4623.74, "stop_loss": 4615.0,
        "warning_level": 4615.0, "sweep_low": 4594.73, "atr15": 12.0,
        "created_at": "2026-08-24T01:00:00+00:00",
        "bars": [
            {"time": "2026-08-24T01:00:00+00:00", "low": 4618, "high": 4625,
             "close": 4624},
            {"time": "2026-08-24T01:15:00+00:00", "low": 4613, "high": 4620,
             "close": 4617},
            {"time": "2026-08-24T01:30:00+00:00", "low": 4616, "high": 4660,
             "close": 4655},
        ],
    }


def test_today_replay_exposes_single_price_false_stop_without_lookahead():
    rows = {row["policy"]: row for row in compare_case(today_case())}
    assert rows["SINGLE_PRICE"]["exitReason"] == "PRICE_TOUCH"
    assert rows["SINGLE_PRICE"]["falseStop"] is True
    assert rows["CLOSE_BASED"]["exitReason"] == "END_OF_REPLAY"
    assert rows["THESIS_BASED"]["exitReason"] == "END_OF_REPLAY"


def test_small_fixture_is_reported_as_insufficient_not_verified():
    report = aggregate_comparison([today_case()], minimum_sample=30)
    assert report["verified"] is False
    metrics = report["strategies"]["SWEEP_RECLAIM_LONG"]
    assert all(item["status"] == "INSUFFICIENT_SAMPLE" for item in metrics.values())

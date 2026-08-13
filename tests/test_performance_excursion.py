from app.services.outcome_tracker import excursion_pct


def test_long_excursion_reports_favorable_and_adverse_path():
    assert excursion_pct("LONG", 100, [103, 102], [99, 97]) == (3.0, -3.0)


def test_short_excursion_inverts_price_path_correctly():
    assert excursion_pct("SHORT", 100, [102, 101], [96, 98]) == (4.0, -2.0)


def test_watch_has_no_excursion():
    assert excursion_pct("WATCH", 100, [101], [99]) is None

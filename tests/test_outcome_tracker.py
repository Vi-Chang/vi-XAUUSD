from app.services.outcome_tracker import signed_return_pct


def test_long_forward_return_is_directional():
    assert signed_return_pct("LONG", 4000, 4040) == 1.0
    assert signed_return_pct("LONG", 4000, 3960) == -1.0


def test_short_forward_return_is_directional():
    assert signed_return_pct("SHORT", 4000, 3960) == 1.0
    assert signed_return_pct("PREPARE_SHORT", 4000, 4040) == -1.0


def test_non_entry_action_has_no_outcome():
    assert signed_return_pct("WATCH", 4000, 4040) is None
    assert signed_return_pct("LONG", 0, 4040) is None

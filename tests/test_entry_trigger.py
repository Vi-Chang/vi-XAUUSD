import pandas as pd

from app.engines.entry_trigger import evaluate_entry_gate
from app.engines.key_levels import CandidateLevel


def _bar(open_, high, low, close):
    return pd.DataFrame([{"open": open_, "high": high, "low": low, "close": close}])


def _level(level_id, kind, low, high, strength="STRONG"):
    return CandidateLevel(level_id, kind, low, high, strength, ["test"])


def _gate(direction, *, levels, entry, previous, bar, price=4000.0, atr=10.0):
    return evaluate_entry_gate(
        direction, price=price, atr15=atr, levels=levels,
        entry_zone_id=entry, previous_action=previous, m15_df=bar,
        opposing_zone_atr_mult=0.75, breakout_buffer_atr_mult=0.10,
    )


def test_long_near_strong_resistance_is_hard_blocked():
    levels = [
        _level("SUP", "SUP_ZONE", 3990, 3995),
        _level("RES", "RES_ZONE", 4005, 4010),
    ]
    gate = _gate("LONG", levels=levels, entry="SUP", previous="PREPARE_LONG",
                 bar=_bar(3992, 4006, 3991, 4004))
    assert gate.blocked is True
    assert gate.triggered is False


def test_close_confirmed_breakout_releases_opposing_zone_gate():
    levels = [
        _level("SUP", "SUP_ZONE", 3990, 3995),
        _level("RES", "RES_ZONE", 4005, 4010),
    ]
    gate = _gate("LONG", levels=levels, entry="SUP", previous="PREPARE_LONG",
                 bar=_bar(3992, 4013, 3991, 4012))
    assert gate.blocked is False
    assert gate.triggered is True


def test_trigger_requires_previous_prepare_state():
    levels = [_level("SUP", "SUP_ZONE", 3990, 3995)]
    gate = _gate("LONG", levels=levels, entry="SUP", previous="WATCH",
                 bar=_bar(3992, 4001, 3991, 4000))
    assert gate.blocked is False
    assert gate.triggered is False


def test_long_trigger_requires_touch_and_bullish_close_above_zone():
    levels = [_level("SUP", "SUP_ZONE", 3990, 3995)]
    good = _gate("LONG", levels=levels, entry="SUP", previous="PREPARE_LONG",
                 bar=_bar(3992, 4001, 3991, 4000))
    weak = _gate("LONG", levels=levels, entry="SUP", previous="PREPARE_LONG",
                 bar=_bar(4000, 4001, 3991, 3994))
    assert good.triggered is True
    assert weak.triggered is False


def test_short_trigger_is_symmetric():
    levels = [_level("RES", "RES_ZONE", 4005, 4010)]
    gate = _gate("SHORT", levels=levels, entry="RES", previous="PREPARE_SHORT",
                 bar=_bar(4008, 4009, 3998, 4000), price=4005)
    assert gate.triggered is True

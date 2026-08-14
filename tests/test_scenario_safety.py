import pytest

from app.engines.scenario_safety import (
    PriceZone,
    calculate_risk_reward,
    conservative_entry,
    lifecycle_status,
    stable_setup_id,
    validate_price_structure,
)

LONG_ENTRY = PriceZone(4368.04, 4377.05)
LONG_TARGETS = [PriceZone(4380.00, 4387.77), PriceZone(4394, 4403), PriceZone(4408, 4414)]


def test_long_stop_inside_entry_is_invalid():
    reasons = validate_price_structure(
        "LONG", entry=LONG_ENTRY, planned_entry=4377.05, stop_loss=4370.09,
        targets=LONG_TARGETS)
    assert reasons == ["停損落在進場區內或其上方，價格結構無效"]


def test_short_stop_inside_entry_is_invalid():
    reasons = validate_price_structure(
        "SHORT", entry=PriceZone(4400, 4410), planned_entry=4400, stop_loss=4405,
        targets=[PriceZone(4380, 4390)])
    assert "停損落在進場區內" in reasons[0]


def test_planned_entry_outside_zone_is_invalid():
    reasons = validate_price_structure(
        "LONG", entry=LONG_ENTRY, planned_entry=4380, stop_loss=4360,
        targets=LONG_TARGETS)
    assert reasons == ["賺賠比計算基準價不在進場區內"]


@pytest.mark.parametrize("price", [4368.04, 4377.05])
def test_entry_zone_boundaries_are_ready(price):
    assert lifecycle_status(
        "LONG", current_price=price, entry=LONG_ENTRY, first_target=LONG_TARGETS[0],
        structure_valid=True, confirmations_passed=True) == "READY"


def test_reported_case_is_expired_not_ready():
    assert lifecycle_status(
        "LONG", current_price=4381.73, entry=LONG_ENTRY, first_target=LONG_TARGETS[0],
        structure_valid=True, confirmations_passed=True) == "EXPIRED"


def test_long_and_short_missed_entry_are_mirrored():
    assert lifecycle_status(
        "LONG", current_price=4378, entry=LONG_ENTRY, first_target=LONG_TARGETS[0],
        structure_valid=True, confirmations_passed=True) == "MISSED_ENTRY_WAIT_RETEST"
    assert lifecycle_status(
        "SHORT", current_price=4395, entry=PriceZone(4400, 4410),
        first_target=PriceZone(4380, 4390), structure_valid=True,
        confirmations_passed=True) == "MISSED_ENTRY_WAIT_RETEST"


def test_conservative_entry_uses_worst_zone_edge():
    assert conservative_entry("LONG", LONG_ENTRY) == 4377.05
    assert conservative_entry("SHORT", PriceZone(4400, 4410)) == 4400


def test_cost_adjusted_rr_is_traceable_and_positive():
    result = calculate_risk_reward(
        "LONG", evaluation_entry_price=4377.05, stop_loss=4360,
        target_price=4380, spread=0.4, slippage=0.1, fees=0.05)
    assert result["available"] is True
    assert result["evaluationEntryPrice"] == 4377.05
    assert result["spread"] == 0.4
    assert result["ratio"] == pytest.approx(result["rewardDistance"] / result["riskDistance"], abs=0.01)


def test_invalid_reward_returns_unavailable():
    result = calculate_risk_reward(
        "LONG", evaluation_entry_price=100, stop_loss=90, target_price=99)
    assert result == {"available": False, "ratio": None,
                      "reason": "有效風險或有效獲利距離不是正值"}


def test_same_breakout_keeps_setup_id_and_new_breakout_changes_it():
    kwargs = {"symbol": "XAUUSD", "direction": "LONG", "timeframe": "15M",
              "trigger_level": "4377.05", "breakout_at": "2026-08-14T12:45:00+00:00"}
    first = stable_setup_id(**kwargs)
    assert stable_setup_id(**kwargs) == first
    assert stable_setup_id(**{**kwargs, "breakout_at": "2026-08-14T13:00:00+00:00"}) != first

"""Directional entry-zone classification shared by every decision gate."""
from __future__ import annotations


def classify_entry_location(direction: str, current_price: float, zone_low: float,
                            zone_high: float, chase_limit: float | None = None) -> str:
    if zone_low > zone_high:
        zone_low, zone_high = zone_high, zone_low
    if zone_low <= current_price <= zone_high:
        return "IN_EXECUTABLE_ZONE"
    side = str(direction).upper()
    if side == "LONG":
        if current_price < zone_low:
            return "WAIT_HIGHER_PRICE"
        return "CHASE_LONG" if chase_limit is not None and current_price > chase_limit else "ABOVE_LONG_ZONE"
    if side == "SHORT":
        if current_price > zone_high:
            return "WAIT_BEARISH_RECONFIRMATION"
        return "CHASE_SHORT" if chase_limit is not None and current_price < chase_limit else "BELOW_SHORT_ZONE"
    return "INVALID_DIRECTION"


def stop_is_valid(direction: str, current_price: float, stop_price: float | None) -> bool:
    if stop_price is None:
        return False
    if str(direction).upper() == "LONG":
        return stop_price < current_price
    if str(direction).upper() == "SHORT":
        return stop_price > current_price
    return False

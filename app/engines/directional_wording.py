"""Direction-aware wording for support and resistance crossings."""
from __future__ import annotations


def format_level_cross(
    *, level_kind: str, movement: str, level: float,
    timeframe: str = "15M", role: str | None = None,
) -> str:
    """Describe a crossing without ambiguous words such as ``穿越``.

    ``movement`` is the price movement (``UP``/``DOWN``), while
    ``level_kind`` describes the level itself (``support``/``resistance``).
    """
    kind = str(level_kind or "").lower()
    move = str(movement or "").upper()
    label = role or ("支撐位" if kind == "support" else "壓力位")
    price = f"{float(level):.2f}"
    if kind == "support" and move == "DOWN":
        verb = "收盤跌破"
    elif kind == "support" and move == "UP":
        verb = "收盤收復"
    elif kind == "resistance" and move == "UP":
        verb = "收盤突破"
    elif kind == "resistance" and move == "DOWN":
        verb = "收盤跌回"
    else:
        verb = "收盤測試"
    return f"{label} {price} 被 {timeframe} {verb}"


def invalidation_wording(direction: str, level: float, timeframe: str = "15M") -> str:
    """Return an explicit invalidation sentence for the proposed direction."""
    if str(direction).upper() == "LONG":
        return f"{timeframe} 收盤跌破 {float(level):.2f}，多方重新失效"
    return f"{timeframe} 收盤站上 {float(level):.2f}，空方重新失效"

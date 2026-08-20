"""Position-free exit guidance recalculated from the latest closed 15M structure."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ExitZone:
    low: float
    high: float
    action: str


@dataclass(frozen=True)
class HypotheticalExitPlan:
    side: str
    partial_exit: ExitZone
    full_exit: ExitZone
    defense_price: float
    candle_close_time: str
    wording: str


def _zones(data: dict, names: tuple[str, ...]) -> list[tuple[float, float]]:
    found: set[tuple[float, float]] = set()
    for name in names:
        for zone in (data.get("key_levels") or {}).get(name) or []:
            low, high = zone.get("price_low"), zone.get("price_high")
            if (
                isinstance(low, (int, float))
                and isinstance(high, (int, float))
                and low <= high
            ):
                found.add((float(low), float(high)))
    return sorted(found)


def build_hypothetical_exit_plans(data: dict) -> list[HypotheticalExitPlan]:
    normalized = data.get("normalized_analysis") or {}
    price = float(normalized.get("currentPrice") or 0)
    atr = max(float(normalized.get("atr15") or 0), 3.0)
    closed = str(normalized.get("lastClosedCandleTimestamp") or "")
    resistances = [
        z
        for z in _zones(data, ("strong_resistance_zones", "weak_resistance_zones"))
        if z[1] > price
    ]
    supports = [
        z
        for z in _zones(data, ("strong_support_zones", "weak_support_zones"))
        if z[0] < price
    ]
    resistances = resistances or [
        (price + atr, price + atr * 1.15),
        (price + atr * 2, price + atr * 2.15),
    ]
    supports = supports or [
        (price - atr * 2.15, price - atr * 2),
        (price - atr * 1.15, price - atr),
    ]
    resistances.sort()
    supports.sort(reverse=True)
    support = next(
        (
            x
            for x in normalized.get("confirmationLevels", [])
            if x.get("kind") == "support" and isinstance(x.get("price"), (int, float))
        ),
        None,
    )
    resistance = next(
        (
            x
            for x in normalized.get("confirmationLevels", [])
            if x.get("kind") == "resistance"
            and isinstance(x.get("price"), (int, float))
        ),
        None,
    )
    long_defense = (
        float(support["price"] - support.get("buffer", 0)) if support else price - atr
    )
    short_defense = (
        float(resistance["price"] + resistance.get("buffer", 0))
        if resistance
        else price + atr
    )
    return [
        HypotheticalExitPlan(
            "LONG",
            ExitZone(*resistances[0], "分批平倉"),
            ExitZone(*resistances[min(1, len(resistances) - 1)], "全部平倉"),
            round(long_defense, 2),
            closed,
            "若你持有多單",
        ),
        HypotheticalExitPlan(
            "SHORT",
            ExitZone(*supports[0], "分批平倉"),
            ExitZone(*supports[min(1, len(supports) - 1)], "全部平倉"),
            round(short_defense, 2),
            closed,
            "若你持有空單",
        ),
    ]


def evaluate_hypothetical_exits(
    data: dict, previous: dict | None = None
) -> tuple[dict, list[dict]]:
    previous = previous or {}
    normalized = data.get("normalized_analysis") or {}
    price = float(normalized.get("currentPrice") or 0)
    closed_price = normalized.get("lastClosedCandlePrice")
    state, events = {}, []
    for plan in build_hypothetical_exit_plans(data):
        old = previous.get(plan.side, {})
        candidates = (plan.partial_exit, plan.full_exit)
        reached = next((z for z in candidates if z.low <= price <= z.high), None)
        nearest = min(
            candidates, key=lambda z: min(abs(price - z.low), abs(price - z.high))
        )
        distance = (
            0.0 if reached else min(abs(price - nearest.low), abs(price - nearest.high))
        )
        reverse_break = isinstance(closed_price, (int, float)) and (
            (plan.side == "LONG" and closed_price < plan.defense_price)
            or (plan.side == "SHORT" and closed_price > plan.defense_price)
        )
        region = "inside" if reached else "approach" if distance <= 3 else "outside"
        episode = int(old.get("episode", 0))
        if (
            region in ("inside", "approach")
            and old.get("region", "outside") == "outside"
        ):
            episode += 1
        event_type = (
            "EXIT_NOW"
            if reverse_break and not old.get("defense_broken")
            else "EXIT_ZONE_REACHED"
            if reached and old.get("region") != "inside"
            else "EXIT_APPROACHING"
            if region == "approach" and old.get("region", "outside") == "outside"
            else ""
        )
        target = reached or nearest
        if event_type:
            action = "全部平倉" if event_type == "EXIT_NOW" else target.action
            events.append(
                {
                    "event_type": event_type,
                    "side": plan.side,
                    "price": price,
                    "zone_low": target.low,
                    "zone_high": target.high,
                    "action": action,
                    "defense_price": plan.defense_price,
                    "candle_close_time": plan.candle_close_time,
                    "topic": f"hypo-exit:{plan.side}:{event_type}:{target.low:.2f}:"
                    f"{plan.candle_close_time}:{episode}",
                    "message": f"【{plan.wording}】現價 {price:.2f}；{action}區 "
                    f"{target.low:.2f}–{target.high:.2f}；防守價 {plan.defense_price:.2f}。",
                }
            )
        state[plan.side] = {
            "region": region,
            "episode": episode,
            "defense_broken": reverse_break,
            "plan": asdict(plan),
        }
    return state, events

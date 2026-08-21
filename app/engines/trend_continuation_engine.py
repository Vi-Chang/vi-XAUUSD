"""Closed-candle, ATR-normalized continuation setups for strong one-way markets."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

import pandas as pd

ENGINE_VERSION = "trend-continuation-v1"
READY_STATES = {
    "ENTRY_READY_SHALLOW_PULLBACK", "ENTRY_READY_BREAKOUT_RETEST",
    "ENTRY_READY_BULL_FLAG", "ENTRY_READY_MOMENTUM_CONTINUATION",
}


def _mirror_frame(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """Mirror OHLC around zero so the proven long rules remain exactly symmetric."""
    if frame is None:
        return None
    mirrored = frame.copy()
    old_high, old_low = frame["high"].astype(float), frame["low"].astype(float)
    mirrored["open"] = -frame["open"].astype(float)
    mirrored["close"] = -frame["close"].astype(float)
    mirrored["high"] = -old_low
    mirrored["low"] = -old_high
    return mirrored


def _unmirror_short(long_result: dict, settings) -> tuple[dict, list[dict]]:
    type_map = {
        "SHALLOW_PULLBACK_LONG": "SHALLOW_PULLBACK_SHORT",
        "BREAKOUT_RETEST_LONG": "BREAKOUT_RETEST_SHORT",
        "BULL_FLAG_CONTINUATION": "BEAR_FLAG_CONTINUATION",
        "MOMENTUM_CONTINUATION": "MOMENTUM_CONTINUATION_SHORT",
    }
    status_map = {
        "ENTRY_READY_SHALLOW_PULLBACK": "ENTRY_READY_SHALLOW_PULLBACK_SHORT",
        "ENTRY_READY_BREAKOUT_RETEST": "ENTRY_READY_BREAKOUT_RETEST_SHORT",
        "ENTRY_READY_BULL_FLAG": "ENTRY_READY_BEAR_FLAG",
        "ENTRY_READY_MOMENTUM_CONTINUATION": "ENTRY_READY_MOMENTUM_CONTINUATION_SHORT",
    }
    mapped: list[dict] = []
    for original in long_result.get("candidates") or []:
        item = dict(original)
        item["type"] = type_map.get(item.get("type"), item.get("type"))
        item["status"] = status_map.get(item.get("status"), item.get("status"))
        item["direction"] = "SHORT"
        for key in ("suggestedEntry", "stopPrice", "tp1", "tp2", "tp3"):
            if item.get(key) is not None:
                item[key] = round(-float(item[key]), 2)
        if item.get("entryZoneLow") is not None:
            low, high = -float(item["entryZoneHigh"]), -float(item["entryZoneLow"])
            item["entryZoneLow"], item["entryZoneHigh"] = round(low, 2), round(high, 2)
        if item.get("setupId"):
            item["setupId"] = _setup_id(item["type"], item.get("createdFromCandleTime") or "", item.get("entryZoneHigh") or 0)
        # Mirroring is an internal calculation detail; user-facing prices must
        # always remain normal positive XAUUSD values.
        for key in ("passedReasons", "missingConditions"):
            item[key] = [re.sub(r"-(\d+(?:\.\d+)?)", r"\1", str(reason))
                         for reason in item.get(key) or []]
        mapped.append(item)
    selected = next((item for item in mapped if str(item.get("status")).startswith("ENTRY_READY_")), None)
    result = {**long_result, "marketType": "TREND_CONTINUATION_SHORT",
              "trendScore": 100 - int(long_result.get("trendScore") or 100),
              "adjustedSignalScore": 100 - int(long_result.get("trendScore") or 100),
              "candidates": mapped, "selected": selected,
              "status": selected.get("status") if selected else "WAIT"}
    events = []
    if selected:
        events.append({"event_type": selected["status"], "setupId": selected["setupId"],
                       "currentState": selected["status"], "direction": "SHORT", "setup": selected,
                       "entryZone": {"low": selected["entryZoneLow"], "high": selected["entryZoneHigh"]},
                       "triggerPrice": selected["entryZoneLow"], "blockedReason": "",
                       "notificationEligible": not settings.trend_continuation_shadow_mode})
    return result, events


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.astype(float).ewm(span=span, adjust=False).mean()


def _atr(frame: pd.DataFrame, period: int = 14) -> float:
    previous = frame["close"].shift(1)
    ranges = pd.concat(((frame["high"] - frame["low"]).abs(),
                        (frame["high"] - previous).abs(),
                        (frame["low"] - previous).abs()), axis=1).max(axis=1)
    return float(ranges.tail(period).mean())


def _trend_dimensions(frame: pd.DataFrame | None) -> dict:
    if frame is None or len(frame) < 30:
        return {"structure": 0, "emaAlignment": 0, "emaSlope": 0, "macd": 0}
    close = frame["close"].astype(float)
    fast, slow = _ema(close, 20), _ema(close, 50)
    macd = _ema(close, 12) - _ema(close, 26)
    recent = frame.tail(8)
    structure = int(recent["high"].tail(4).max() >= recent["high"].head(4).max()
                    and recent["low"].tail(4).min() >= recent["low"].head(4).min())
    return {"structure": 100 * structure,
            "emaAlignment": 100 if fast.iloc[-1] > slow.iloc[-1] else 0,
            "emaSlope": 100 if fast.iloc[-1] > fast.iloc[-4] else 0,
            "macd": 100 if macd.iloc[-1] > 0 else 0}


def classify_market_type(h1: pd.DataFrame | None, h4: pd.DataFrame | None) -> tuple[str, int, dict]:
    h4d, h1d = _trend_dimensions(h4), _trend_dimensions(h1)
    weights = {"h4Structure": .25, "h4Trend": .25, "h1Structure": .25,
               "h1Trend": .15, "momentum": .10}
    parts = {"h4Structure": h4d["structure"],
             "h4Trend": (h4d["emaAlignment"] + h4d["emaSlope"]) / 2,
             "h1Structure": h1d["structure"],
             "h1Trend": (h1d["emaAlignment"] + h1d["emaSlope"]) / 2,
             "momentum": (h4d["macd"] + h1d["macd"]) / 2}
    bullish = round(sum(parts[key] * weight for key, weight in weights.items()))
    h4_score = (h4d["structure"] + h4d["emaAlignment"] + h4d["emaSlope"] + h4d["macd"]) / 4
    h1_score = (h1d["structure"] + h1d["emaAlignment"] + h1d["emaSlope"] + h1d["macd"]) / 4
    if (h4_score >= 70 and h1_score <= 30) or (h4_score <= 30 and h1_score >= 70):
        market_type = "REVERSAL"
    elif bullish >= 70:
        market_type = "TREND_CONTINUATION_LONG"
    elif bullish <= 30:
        market_type = "TREND_CONTINUATION_SHORT"
    elif 40 <= bullish <= 60:
        market_type = "RANGE"
    else:
        market_type = "UNDEFINED"
    return market_type, bullish, {**parts, "h4Score": h4_score,
                                  "h1Score": h1_score, "weights": weights}


def _setup_id(kind: str, candle_time: str, anchor: float) -> str:
    raw = f"XAUUSD|{kind}|{candle_time}|{anchor:.2f}"
    return f"TC-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _plan(kind: str, status: str, *, candle_time: str, zone_low: float,
          zone_high: float, stop: float, current: float, atr: float,
          score: int, reasons: list[str], missing: list[str], settings,
          risk_weight: float = 1.0, execution_cost: float = 0.0,
          expiry_bars: int | None = None, max_chase_mult: float | None = None) -> dict:
    entry = min(max(current, zone_low), zone_high)
    risk = entry - stop
    # Targets compensate for round-trip spread/slippage, so the displayed R is
    # executable net R rather than a frictionless chart distance.
    tp1 = entry + risk * 1.5 + execution_cost * 2.5
    tp2 = entry + risk * 2 + execution_cost * 3
    tp3 = entry + risk * 3 + execution_cost * 4
    rr = ((tp1 - entry - execution_cost) / (risk + execution_cost)
          if risk > 0 else 0)
    if risk <= 0 and status.startswith("ENTRY_READY_"):
        status = "WAIT_RISK_PLAN"
        missing = [*missing, f"防守價 {stop:.2f} 未低於多單進場價 {entry:.2f}"]
    expiry_bars = expiry_bars or settings.trend_setup_expiry_bars
    max_chase_mult = (settings.trend_max_chase_atr_mult
                      if max_chase_mult is None else max_chase_mult)
    expires = (datetime.fromisoformat(candle_time.replace("Z", "+00:00"))
               + timedelta(minutes=15 * expiry_bars)).isoformat()
    return {"setupId": _setup_id(kind, candle_time, zone_high), "setupVersion": ENGINE_VERSION,
            "type": kind, "direction": "LONG", "status": status,
            "createdFromCandleTime": candle_time, "entryZoneLow": round(zone_low, 2),
            "entryZoneHigh": round(zone_high, 2), "suggestedEntry": round(entry, 2),
            "stopPrice": round(stop, 2), "tp1": round(tp1, 2),
            "tp2": round(tp2, 2), "tp3": round(tp3, 2),
            "riskReward": round(rr, 2), "grossRiskReward": 1.5,
            "executionCost": round(execution_cost, 4),
            "maxChaseDistance": round(atr * max_chase_mult, 2),
            "signalScore": score, "riskWeight": risk_weight, "passedReasons": reasons,
            "missingConditions": missing, "expiresAt": expires,
            "atrValue": round(atr, 4), "atrTimeframe": "15M",
            "stopSource": "15M_STRUCTURE_WITH_ATR_BUFFER",
            "targetSource": "STRUCTURE_FIRST_R_MULTIPLE_FALLBACK",
            "calculationVersion": ENGINE_VERSION}


def evaluate_trend_continuation(data: dict, *, m15: pd.DataFrame | None,
                                h1: pd.DataFrame | None, h4: pd.DataFrame | None,
                                previous: dict | None = None,
                                _mirrored: bool = False,
                                _profile: str = "LONG") -> tuple[dict, list[dict]]:
    from app.config import get_settings

    settings = get_settings()
    previous = previous or {}
    if not settings.trend_continuation_enabled or m15 is None or len(m15) < 20:
        return {"enabled": settings.trend_continuation_enabled, "shadowMode": True,
                "marketType": "UNDEFINED", "candidates": [],
                "reason": "15M 歷史不足 20 根", "version": ENGINE_VERSION}, []
    frame = m15[m15.get("is_closed", True).astype(bool)] if "is_closed" in m15 else m15
    if len(frame) < 20:
        return {"enabled": True, "shadowMode": True, "marketType": "UNDEFINED",
                "candidates": [], "reason": "已收盤15M不足20根", "version": ENGINE_VERSION}, []
    market_type, trend_score, dimensions = classify_market_type(h1, h4)
    if market_type == "TREND_CONTINUATION_SHORT" and not _mirrored:
        mirrored, _ = evaluate_trend_continuation(
            data, m15=_mirror_frame(frame), h1=_mirror_frame(h1), h4=_mirror_frame(h4),
            previous=None, _mirrored=True, _profile="SHORT")
        return _unmirror_short(mirrored, settings)
    is_short_profile = _profile == "SHORT"
    shallow_zone_mult = (settings.trend_short_shallow_zone_atr_mult if is_short_profile
                         else settings.trend_shallow_zone_atr_mult)
    max_chase_mult = (settings.trend_short_max_chase_atr_mult if is_short_profile
                      else settings.trend_max_chase_atr_mult)
    momentum_chase_mult = (settings.trend_short_momentum_max_chase_atr_mult
                           if is_short_profile else settings.trend_momentum_max_chase_atr_mult)
    flag_range_mult = (settings.trend_short_flag_max_range_atr_mult if is_short_profile
                       else settings.trend_flag_max_range_atr_mult)
    flag_impulse_mult = (settings.trend_short_flag_min_impulse_atr_mult if is_short_profile
                         else settings.trend_flag_min_impulse_atr_mult)
    expiry_bars = (settings.trend_short_setup_expiry_bars if is_short_profile
                   else settings.trend_setup_expiry_bars)
    atr = max(_atr(frame), 0.01)
    last, prior = frame.iloc[-1], frame.iloc[-2]
    current, closed = float(last["close"]), float(last["close"])
    candle_time = str(last.get("close_time") or last.get("open_time") or
                      data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    normalized = data.get("normalized_analysis") or {}
    rsi = float(((data.get("timeframes") or {}).get("m15") or {}).get("rsi") or 50)
    overbought = rsi >= 80
    score = max(0, trend_score - (settings.trend_overbought_penalty if overbought else 0))
    recent_low = float(frame["low"].tail(8).min())
    ema20 = float(_ema(frame["close"], 20).iloc[-1])
    support = max(recent_low, ema20)
    half = atr * shallow_zone_mult
    shallow_low, shallow_high = support - half, support + half
    bullish_close = closed > float(last["open"])
    higher_low = float(last["low"]) > float(prior["low"])
    broke_prior_high = closed > float(prior["high"])
    data_good = normalized.get("marketDataStatus") == "GOOD"
    spread = float((data.get("current_price") or {}).get("spread") or 0)
    execution_cost = max(0.0, spread) + settings.estimated_slippage_abs
    from app.engines.execution_context import market_session
    session = market_session(candle_time)
    event = data.get("event_risk") or {}
    event_impact = str(event.get("event_impact") or "UNKNOWN").upper()
    time_risk = str(event.get("time_risk") or event.get("level") or "UNKNOWN").upper()
    event_source = str(event.get("source") or "none").lower()
    event_lockout = bool(event.get("event_lockout") or event.get("post_event_wait"))
    event_unknown = event_source == "none" or event_impact == "UNKNOWN" or time_risk == "UNKNOWN"
    event_high_risk = event_lockout or (event_impact == "HIGH" and time_risk == "HIGH")
    event_penalty = (settings.trend_event_high_risk_score_penalty if event_high_risk
                     else settings.trend_event_unknown_score_penalty if event_unknown else 0)
    if event_high_risk:
        expiry_bars = min(expiry_bars, settings.trend_event_high_risk_expiry_bars)
    score_threshold = (settings.trend_continuation_min_score
                       + settings.session_score_adjustments.get(session["name"], 0)
                       + event_penalty)
    spread_limit = max(settings.gate_spread_max_abs,
                       atr * settings.gate_spread_max_atr15_mult)
    execution_good = spread <= spread_limit
    common = (market_type == "TREND_CONTINUATION_LONG"
              and score >= score_threshold
              and data_good and execution_good and not event_lockout)
    candidates: list[dict] = []

    shallow_in_zone = shallow_low <= current <= shallow_high
    shallow_confirmed = shallow_in_zone and bullish_close and (higher_low or broke_prior_high)
    shallow_missing = []
    if not common: shallow_missing.append(f"趨勢評分 {score}，{session['name']} 時段門檻 {score_threshold}")
    if event_lockout: shallow_missing.append("重大事件凍結中，暫停建立新倉")
    elif event_unknown: shallow_missing.append(f"事件資料未知，訊號門檻提高 {event_penalty} 分")
    if not shallow_in_zone: shallow_missing.append(f"距回踩區 {min(abs(current-shallow_low), abs(current-shallow_high)):.2f}，需進入 {shallow_low:.2f}–{shallow_high:.2f}")
    if shallow_in_zone and not shallow_confirmed: shallow_missing.append("尚缺15M止跌、HL或突破前K高點確認")
    candidates.append(_plan("SHALLOW_PULLBACK_LONG", "ENTRY_READY_SHALLOW_PULLBACK" if common and shallow_confirmed else "WAIT_SHALLOW_PULLBACK",
                            candle_time=candle_time, zone_low=shallow_low, zone_high=shallow_high,
                            stop=recent_low - atr * .15, current=current, atr=atr, score=score,
                            reasons=["4H／1H趨勢同向", "15M結構未破壞"] + (["超買僅降低權重"] if overbought else []),
                            missing=shallow_missing, settings=settings, execution_cost=execution_cost,
                            expiry_bars=expiry_bars, max_chase_mult=max_chase_mult))

    ledger = data.get("breakout_setup_manager") or {}
    fixed = next((s for s in reversed(ledger.get("setups") or [])
                  if s.get("direction") == "LONG" and s.get("breakoutConfirmedAt")), None)
    if fixed:
        zlow, zhigh = float(fixed["retestZoneLow"]), float(fixed["retestZoneHigh"])
        in_zone = zlow <= current <= zhigh
        confirmed = in_zone and bullish_close and closed >= float(fixed["breakoutTrigger"])
        missing = [] if confirmed else [f"等待回踩固定區 {zlow:.2f}–{zhigh:.2f} 並由15M收盤守住"]
        candidates.append(_plan("BREAKOUT_RETEST_LONG", "ENTRY_READY_BREAKOUT_RETEST" if common and confirmed else "WAIT_BREAKOUT_RETEST",
                                candle_time=str(fixed.get("createdFromCandleTime") or candle_time), zone_low=zlow, zone_high=zhigh,
                                stop=float(fixed["stopPrice"]), current=current, atr=atr, score=score,
                                reasons=[f"固定突破 {float(fixed['breakoutTrigger']):.2f} 已確認"], missing=missing,
                                settings=settings, execution_cost=execution_cost,
                                expiry_bars=expiry_bars, max_chase_mult=max_chase_mult))
    else:
        candidates.append({"type": "BREAKOUT_RETEST_LONG", "status": "NO_SETUP",
                           "missingConditions": ["目前沒有已確認且可回踩的固定突破劇本"]})

    consolidation = frame.iloc[-5:-1]
    impulse = float(frame.iloc[-9:-5]["close"].iloc[-1] - frame.iloc[-9:-5]["open"].iloc[0])
    flag_range = float(consolidation["high"].max() - consolidation["low"].min())
    flag_high, flag_low = float(consolidation["high"].max()), float(consolidation["low"].min())
    first_half_range = float(consolidation.iloc[:2]["high"].max() - consolidation.iloc[:2]["low"].min())
    last_half_range = float(consolidation.iloc[2:]["high"].max() - consolidation.iloc[2:]["low"].min())
    contracting = last_half_range <= first_half_range
    flag_ok = (common and impulse >= atr * flag_impulse_mult
               and flag_range <= atr * flag_range_mult
               and contracting
               and closed > flag_high and current <= flag_high + atr * max_chase_mult)
    flag_missing = []
    if impulse < atr * flag_impulse_mult: flag_missing.append(f"前段漲幅 {impulse:.2f}，門檻 {atr * flag_impulse_mult:.2f}")
    if flag_range > atr * flag_range_mult: flag_missing.append(f"整理寬度 {flag_range:.2f}，上限 {atr * flag_range_mult:.2f}")
    if not contracting: flag_missing.append(f"整理波動未收斂：後半 {last_half_range:.2f}，前半 {first_half_range:.2f}")
    if closed <= flag_high: flag_missing.append(f"15M收盤 {closed:.2f} 尚未突破旗形高點 {flag_high:.2f}")
    if current > flag_high + atr * max_chase_mult: flag_missing.append(f"旗形突破延伸 {current - flag_high:.2f}，最大允許 {atr * max_chase_mult:.2f}")
    candidates.append(_plan("BULL_FLAG_CONTINUATION", "ENTRY_READY_BULL_FLAG" if flag_ok else "WAIT_BULL_FLAG",
                            candle_time=candle_time, zone_low=flag_high, zone_high=flag_high + atr * .15,
                            stop=flag_low - atr * .10, current=current, atr=atr, score=score,
                            reasons=["窄幅整理維持多方結構"], missing=flag_missing, settings=settings,
                            execution_cost=execution_cost, expiry_bars=expiry_bars,
                            max_chase_mult=max_chase_mult))

    breakout = float(frame["high"].iloc[-6:-1].max())
    chase = current - breakout
    momentum_ok = (market_type == "TREND_CONTINUATION_LONG" and trend_score >= settings.trend_continuation_strong_score
                   and closed > breakout and chase <= atr * momentum_chase_mult
                   and bullish_close and data_good and execution_good
                   and not event_lockout and not event_unknown)
    momentum_missing = []
    if trend_score < settings.trend_continuation_strong_score: momentum_missing.append(f"趨勢評分 {trend_score}，動能門檻 {settings.trend_continuation_strong_score}")
    if closed <= breakout: momentum_missing.append(f"15M收盤 {closed:.2f} 尚未突破固定高點 {breakout:.2f}")
    if chase > atr * momentum_chase_mult: momentum_missing.append(f"突破延伸 {chase:.2f}，最大允許 {atr * momentum_chase_mult:.2f}")
    if event_lockout: momentum_missing.append("重大事件凍結中，動能進場停用")
    elif event_unknown: momentum_missing.append("事件資料未知，較積極的動能進場停用")
    candidates.append(_plan("MOMENTUM_CONTINUATION", "ENTRY_READY_MOMENTUM_CONTINUATION" if momentum_ok else "WAIT_MOMENTUM",
                            candle_time=candle_time, zone_low=breakout, zone_high=breakout + atr * momentum_chase_mult,
                            stop=float(frame["low"].tail(3).min()) - atr * .10, current=current, atr=atr,
                            score=score, reasons=["4H／1H高度一致", "動能型風險較高"],
                            missing=momentum_missing, settings=settings, risk_weight=.5,
                            execution_cost=execution_cost, expiry_bars=expiry_bars,
                            max_chase_mult=momentum_chase_mult))

    # A waiting setup owns immutable prices. Quote/candle refresh may satisfy it,
    # but may not move its zone, stop, targets or chase boundary.
    prior_by_type = {item.get("type"): item for item in previous.get("candidates") or []}
    immutable = ("setupId", "createdFromCandleTime", "entryZoneLow", "entryZoneHigh",
                 "suggestedEntry", "stopPrice", "tp1", "tp2", "tp3",
                 "maxChaseDistance", "expiresAt", "atrValue", "atrTimeframe")
    ready_for_type = {"SHALLOW_PULLBACK_LONG": "ENTRY_READY_SHALLOW_PULLBACK",
                      "BREAKOUT_RETEST_LONG": "ENTRY_READY_BREAKOUT_RETEST",
                      "BULL_FLAG_CONTINUATION": "ENTRY_READY_BULL_FLAG",
                      "MOMENTUM_CONTINUATION": "ENTRY_READY_MOMENTUM_CONTINUATION"}
    for candidate in candidates:
        prior_candidate = prior_by_type.get(candidate.get("type")) or {}
        if not str(prior_candidate.get("status") or "").startswith("WAIT_"):
            continue
        try:
            alive = datetime.fromisoformat(candle_time.replace("Z", "+00:00")) < datetime.fromisoformat(
                str(prior_candidate.get("expiresAt")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            alive = False
        if not alive:
            continue
        candidate.update({key: prior_candidate[key] for key in immutable if key in prior_candidate})
        low, high = float(candidate["entryZoneLow"]), float(candidate["entryZoneHigh"])
        candidate["status"] = prior_candidate["status"]
        in_fixed_zone = low <= current <= high
        kind = str(candidate["type"])
        confirmed_now = False
        if kind in {"SHALLOW_PULLBACK_LONG", "BREAKOUT_RETEST_LONG"}:
            confirmed_now = common and in_fixed_zone and bullish_close and (higher_low or broke_prior_high)
        elif kind == "BULL_FLAG_CONTINUATION":
            confirmed_now = common and closed > low and current <= high + float(candidate["maxChaseDistance"])
        elif kind == "MOMENTUM_CONTINUATION":
            confirmed_now = (trend_score >= settings.trend_continuation_strong_score
                             and closed > low and current <= high and bullish_close and data_good
                             and execution_good and not event_lockout and not event_unknown)
        if confirmed_now:
            candidate["status"] = ready_for_type[kind]
            candidate["missingConditions"] = []
        else:
            distance = 0.0 if in_fixed_zone else min(abs(current - low), abs(current - high))
            candidate["missingConditions"] = [
                f"等待固定進場區 {low:.2f}–{high:.2f}；目前距離 {distance:.2f}"]

    priority = ["ENTRY_READY_SHALLOW_PULLBACK", "ENTRY_READY_BULL_FLAG",
                "ENTRY_READY_BREAKOUT_RETEST", "ENTRY_READY_MOMENTUM_CONTINUATION"]
    selected = next((c for state in priority for c in candidates if c.get("status") == state), None)
    previous_selected = (previous.get("selected") or {}).get("setupId")
    events = []
    if selected and selected["setupId"] != previous_selected:
        events.append({"event_type": selected["status"], "setupId": selected["setupId"],
                       "currentState": selected["status"], "direction": "LONG",
                       "setup": selected,
                       "entryZone": {"low": selected["entryZoneLow"],
                                     "high": selected["entryZoneHigh"]},
                       "triggerPrice": selected["entryZoneHigh"],
                       "blockedReason": "",
                       "notificationEligible": not settings.trend_continuation_shadow_mode})
    result = {"enabled": True, "shadowMode": settings.trend_continuation_shadow_mode,
              "marketType": market_type, "trendScore": trend_score,
              "adjustedSignalScore": score, "overbought": overbought,
              "dimensions": dimensions, "candidates": candidates, "selected": selected,
              "status": selected["status"] if selected else "WAIT",
              "version": ENGINE_VERSION, "evaluatedCandleTime": candle_time,
              "atrValue": round(atr, 4), "atrTimeframe": "15M",
              "marketSession": session, "requiredSignalScore": score_threshold,
              "executionCost": round(execution_cost, 4),
              "parameterProfile": _profile,
              "eventGate": {"impact": event_impact, "timeRisk": time_risk,
                            "source": event_source, "unknown": event_unknown,
                            "lockout": event_lockout, "scorePenalty": event_penalty,
                            "effectiveExpiryBars": expiry_bars}}
    result["execution"] = {"spread": spread, "spreadLimit": round(spread_limit, 4),
                           "passed": execution_good}
    return result, events

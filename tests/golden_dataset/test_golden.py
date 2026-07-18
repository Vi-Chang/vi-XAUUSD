"""Golden Dataset 驗收測試(spec 六):命中率 < 門檻即失敗。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.config import get_settings
from app.engines.market_structure import analyze_structure

DATASET_DIR = Path(__file__).parent
FILES = sorted(DATASET_DIR.glob("*.json"))

SWING_BAR_TOLERANCE = 1     # swing:±1 根 K 棒
EVENT_BAR_TOLERANCE = 2     # BOS/CHoCH/假突破:±2 根
PRICE_TOLERANCE_PCT = 0.002


def _load(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    candles = data["candles"]
    df = pd.DataFrame(
        {k: [c[k] for c in candles] for k in ("open", "high", "low", "close")},
        index=pd.DatetimeIndex([datetime.fromisoformat(c["open_time"]) for c in candles],
                               name="open_time"))
    df["volume"] = 0.0
    df["is_closed"] = True
    return data, df


def _bar_minutes(timeframe: str) -> int:
    return {"5M": 5, "15M": 15, "30M": 30, "1H": 60, "4H": 240}.get(timeframe, 15)


@pytest.mark.parametrize("path", FILES, ids=[f.stem for f in FILES])
def test_golden_dataset_hit_rate(path: Path):
    s = get_settings()
    data, df = _load(path)
    params = data["meta"].get("params", {})
    rep = analyze_structure(df, data["meta"].get("timeframe", "15M"),
                            left=params.get("left", s.swing_left_bars),
                            right=params.get("right", s.swing_right_bars),
                            min_atr_mult=s.swing_min_atr_mult,
                            min_move_pct=s.swing_min_move_pct,
                            fail_confirm_bars=s.false_break_confirm_bars,
                            min_break_atr_mult=s.false_break_min_atr_mult)
    ann = data["annotations"]
    bar = timedelta(minutes=_bar_minutes(data["meta"].get("timeframe", "15M")))

    hits, total, misses = 0, 0, []

    def match_swing(kind: str, entries: list[dict]):
        nonlocal hits, total
        pool = [sp for sp in rep.swings if sp.kind == kind]
        for e in entries:
            total += 1
            t = datetime.fromisoformat(e["time"])
            ok = any(abs((sp.time - t).total_seconds()) <= SWING_BAR_TOLERANCE * bar.total_seconds()
                     and abs(sp.price - e["price"]) <= PRICE_TOLERANCE_PCT * e["price"]
                     for sp in pool)
            if ok:
                hits += 1
            else:
                misses.append(f"{kind}@{e['time']}")

    match_swing("SWING_HIGH", ann.get("swing_highs", []))
    match_swing("SWING_LOW", ann.get("swing_lows", []))

    def match_events(entries: list[dict], type_filter):
        nonlocal hits, total
        for e in entries:
            total += 1
            t = datetime.fromisoformat(e["time"])
            pool = [ev for ev in rep.events if type_filter(ev, e)]
            ok = any(abs((ev.time - t).total_seconds()) <= EVENT_BAR_TOLERANCE * bar.total_seconds()
                     for ev in pool)
            if ok:
                hits += 1
            else:
                misses.append(f"event@{e['time']}")

    match_events(ann.get("bos", []),
                 lambda ev, e: ev.still_valid
                 and ev.event_type.startswith(("BOS", "CHOCH"))
                 and ev.event_type.endswith(f"_{e.get('direction', 'UP')}"))
    match_events(ann.get("choch", []),
                 lambda ev, e: ev.event_type.startswith("CHOCH")
                 and ev.event_type.endswith(f"_{e.get('direction', 'UP')}"))
    match_events(ann.get("false_breakouts", []),
                 lambda ev, e: ev.event_type == e.get("kind", ev.event_type))

    assert total > 0, f"{path.name}: 無任何標註"
    hit_rate = hits / total
    assert hit_rate >= s.golden_dataset_min_hit_rate, (
        f"{path.name}: hit rate {hit_rate:.2%} < {s.golden_dataset_min_hit_rate:.0%}; "
        f"misses={misses}")

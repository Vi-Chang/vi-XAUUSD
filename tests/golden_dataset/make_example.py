"""產生合成 zigzag 範例 Golden Dataset(轉折點=數學 ground truth,不經過引擎)。

用法:python tests/golden_dataset/make_example.py
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

WICK = 0.3
START = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
SEGMENTS = [(20, 2.0), (10, -1.5), (20, 2.0), (10, -1.5), (28, 2.2)]  # (bars, step)
BASE = 4600.0


def main() -> None:
    closes = [BASE]
    turn_idx = []
    i = 0
    for seg_n, (bars, step) in enumerate(SEGMENTS):
        for _ in range(bars):
            closes.append(closes[-1] + step)
            i += 1
        if seg_n < len(SEGMENTS) - 1:
            turn_idx.append((i, "HIGH" if step > 0 else "LOW"))

    candles = []
    prev = closes[0]
    for k, c in enumerate(closes):
        o = prev
        candles.append({
            "open_time": (START + timedelta(minutes=15 * k)).isoformat(),
            "open": round(o, 2), "high": round(max(o, c) + WICK, 2),
            "low": round(min(o, c) - WICK, 2), "close": round(c, 2),
        })
        prev = c

    swing_highs, swing_lows = [], []
    for idx, kind in turn_idx:
        entry = {"time": candles[idx]["open_time"],
                 "price": candles[idx]["high"] if kind == "HIGH" else candles[idx]["low"]}
        (swing_highs if kind == "HIGH" else swing_lows).append(entry)

    # BOS ground truth:最後一段中,收盤首次明確越過前一個 swing high(含 wick)
    prev_peak_high = max(h["price"] for h in swing_highs)
    last_seg_start = turn_idx[-1][0]
    bos = []
    for k in range(last_seg_start + 1, len(candles)):
        if candles[k]["close"] > prev_peak_high + 0.6 and \
                candles[k - 1]["close"] <= prev_peak_high + 0.6:
            bos.append({"time": candles[k]["open_time"], "direction": "UP"})
            break

    out = {
        "meta": {"symbol": "XAUUSD", "timeframe": "15M",
                 "description": "合成 zigzag:上-下-上-下-上,轉折點為數學 ground truth",
                 "annotator": "synthetic", "params": {"left": 2, "right": 2}},
        "candles": candles,
        "annotations": {"swing_highs": swing_highs, "swing_lows": swing_lows,
                        "bos": bos, "choch": [], "false_breakouts": []},
    }
    path = Path(__file__).parent / "example_zigzag_15m.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {path} ({len(candles)} candles, "
          f"{len(swing_highs)}H/{len(swing_lows)}L swings, {len(bos)} bos)")


if __name__ == "__main__":
    main()

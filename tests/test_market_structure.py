"""市場結構引擎(spec 六、七):合成 zigzag 的 swing / BOS / 假跌破。"""
from app.engines.market_structure import analyze_structure, detect_swings
from tests.helpers import make_df, zigzag_path


def test_swings_detected_at_turning_points():
    # 上 20 → 下 10 → 上 20 → 下 10:轉折點即 ground-truth swing
    closes = zigzag_path([(20, 2.0), (10, -1.5), (20, 2.0), (10, -1.5), (20, 2.0)])
    df = make_df(closes)
    swings = detect_swings(df, left=2, right=2, min_atr_mult=0.5, min_move_pct=0.0008)
    kinds = [s.kind for s in swings]
    assert "SWING_HIGH" in kinds and "SWING_LOW" in kinds
    # 交替性
    for a, b in zip(swings, swings[1:]):
        assert a.kind != b.kind
    # 轉折 index:20, 30, 50, 60(±1 容忍 wick 影響)
    high_idx = [s.index for s in swings if s.kind == "SWING_HIGH"]
    assert any(abs(i - 20) <= 1 for i in high_idx)
    assert any(abs(i - 50) <= 1 for i in high_idx)


def test_uptrend_labels_hh_hl():
    closes = zigzag_path([(20, 2.0), (8, -1.0), (20, 2.0), (8, -1.0), (20, 2.0)])
    rep = analyze_structure(make_df(closes), "15M")
    labels = [s.label for s in rep.swings if s.label]
    assert rep.trend == "UP"
    assert "HH" in labels and "HL" in labels
    assert "LL" not in labels


def test_bos_up_recorded_on_break():
    # 盤整後突破前高 → 應有有效 *_UP 突破事件
    closes = zigzag_path([(15, 1.5), (10, -1.8), (15, 1.5), (10, -1.8), (25, 2.5)])
    rep = analyze_structure(make_df(closes), "15M")
    breaks = [e for e in rep.events if e.still_valid and e.event_type.endswith("_UP")]
    assert breaks, [e.event_type for e in rep.events]
    for e in breaks:
        assert e.confirming_candles  # 必須記錄確認 K 棒(spec 六)


def test_failed_breakdown_detected():
    # 跌破前低後 2 根內收回 → FAILED_BREAKDOWN,且原跌破事件失效
    closes = (zigzag_path([(15, 1.5), (10, -1.5), (15, 1.5)])            # 建立前低 ~4007.5
              + [4020, 4010, 4000, 3995]                                  # 急跌破前低
              + [4012, 4022, 4030, 4035, 4040, 4046, 4052])              # 快速收回+走高
    rep = analyze_structure(make_df(closes), "15M", fail_confirm_bars=3)
    types = [e.event_type for e in rep.events]
    assert "FAILED_BREAKDOWN" in types, types
    downs = [e for e in rep.events if e.event_type.endswith("_DOWN")]
    assert any(not e.still_valid for e in downs)


def test_downtrend_trend_down():
    closes = zigzag_path([(15, -2.0), (8, 1.0), (15, -2.0), (8, 1.0), (15, -2.0)])
    rep = analyze_structure(make_df(closes), "1H")
    assert rep.trend == "DOWN"

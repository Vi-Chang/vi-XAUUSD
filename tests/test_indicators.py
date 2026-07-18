"""指標引擎正確性(spec 五)。"""
import numpy as np

from app.engines import indicators as ind
from tests.helpers import make_df, zigzag_path


def sample_df():
    closes = zigzag_path([(60, 1.0), (40, -0.8), (60, 1.2), (40, -0.5), (60, 0.9)])
    return make_df(closes)


def test_compute_all_columns_and_bounds():
    df = sample_df()
    out = ind.compute_all(df)
    for col in ("ema20", "ema50", "ema100", "ema200", "sma20", "macd_dif", "macd_signal",
                "macd_hist", "rsi14", "stoch_k", "stoch_d", "atr14", "adx",
                "bb_upper", "bb_lower", "supertrend", "ichimoku_base", "rel_volume"):
        assert col in out.columns, col
    rsi = out["rsi14"].dropna()
    assert ((rsi >= 0) & (rsi <= 100)).all()
    stoch = out["stoch_k"].dropna()
    assert ((stoch >= -1e-9) & (stoch <= 100 + 1e-9)).all()
    assert (out["atr14"].dropna() > 0).all()


def test_macd_hist_is_dif_minus_signal():
    out = ind.compute_all(sample_df())
    diff = (out["macd_hist"] - (out["macd_dif"] - out["macd_signal"])).abs().dropna()
    assert (diff < 1e-9).all()


def test_rsi_uptrend_above_50():
    df = make_df(zigzag_path([(120, 1.0)]))
    out = ind.compute_all(df)
    assert out["rsi14"].iloc[-1] > 65


def test_bollinger_contains_sma():
    out = ind.compute_all(sample_df())
    tail = out.dropna(subset=["bb_upper", "bb_lower", "bb_mid"]).tail(50)
    assert (tail["bb_upper"] >= tail["bb_mid"]).all()
    assert (tail["bb_lower"] <= tail["bb_mid"]).all()


def test_snapshot_serializable():
    snap = ind.latest_snapshot(ind.compute_all(sample_df()))
    assert snap["rsi14"] is not None
    assert all(v is None or isinstance(v, (int, float, bool)) for v in snap.values())
    assert not any(isinstance(v, np.generic) for v in snap.values())

"""技術指標引擎(spec 五)— 全部由 Python 計算,AI 不得計算指標。

輸入:pandas DataFrame,index 為 open_time(UTC),欄位 open/high/low/close/volume。
Volume 為 Tick Volume,僅在同一資料源內做相對比較。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    dif = ema(close, fast) - ema(close, slow)
    sig = dif.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd_dif": dif, "macd_signal": sig, "macd_hist": dif - sig})


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    # loss=0(純上漲)→ rs=inf → RSI=100;gain=loss=0 → NaN → 中性 50
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss
    return (100 - 100 / (1 + rs)).fillna(50.0)


def stochastic(df: pd.DataFrame, k: int = 14, smooth_k: int = 1, d: int = 3) -> pd.DataFrame:
    low_min = df["low"].rolling(k).min()
    high_max = df["high"].rolling(k).max()
    raw_k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    k_line = raw_k.rolling(smooth_k).mean()
    d_line = k_line.rolling(d).mean()
    return pd.DataFrame({"stoch_k": k_line, "stoch_d": d_line})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr_s = true_range(df).ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr_s
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di,
                         "adx": dx.ewm(alpha=1 / period, adjust=False).mean()})


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    std = close.rolling(period).std()
    return pd.DataFrame({
        "bb_mid": mid, "bb_upper": mid + mult * std, "bb_lower": mid - mult * std,
        "bb_width": (2 * mult * std) / mid,
    })


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    atr_s = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + mult * atr_s
    lower = hl2 - mult * atr_s
    st = pd.Series(np.nan, index=df.index)
    direction = pd.Series(1, index=df.index)
    for i in range(1, len(df)):
        u = min(upper.iloc[i], upper.iloc[i - 1]) if df["close"].iloc[i - 1] < upper.iloc[i - 1] else upper.iloc[i]
        lo = max(lower.iloc[i], lower.iloc[i - 1]) if df["close"].iloc[i - 1] > lower.iloc[i - 1] else lower.iloc[i]
        upper.iloc[i], lower.iloc[i] = u, lo
        if df["close"].iloc[i] > u:
            direction.iloc[i] = 1
        elif df["close"].iloc[i] < lo:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]
        st.iloc[i] = lo if direction.iloc[i] == 1 else u
    return pd.DataFrame({"supertrend": st, "supertrend_dir": direction})


def ichimoku(df: pd.DataFrame) -> pd.DataFrame:
    conv = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2
    base = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
    span_a = ((conv + base) / 2).shift(26)
    span_b = ((df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2).shift(26)
    return pd.DataFrame({"ichimoku_conv": conv, "ichimoku_base": base,
                         "ichimoku_span_a": span_a, "ichimoku_span_b": span_b})


def vwap_by_trading_day(df: pd.DataFrame, trading_days: pd.Series) -> pd.Series:
    """以交易日(NY 17:00 切分)為錨的 VWAP;volume 為 Tick Volume。"""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    cum_pv = pv.groupby(trading_days).cumsum()
    cum_v = df["volume"].groupby(trading_days).cumsum().replace(0, np.nan)
    return cum_pv / cum_v


def compute_all(df: pd.DataFrame, trading_days: pd.Series | None = None) -> pd.DataFrame:
    """計算全部指標,回傳與輸入同 index 的 DataFrame。"""
    out = pd.DataFrame(index=df.index)
    close = df["close"]
    for p in (20, 50, 100, 200):
        out[f"ema{p}"] = ema(close, p)
    out["sma20"] = sma(close, 20)
    out = out.join(macd(close))
    out["rsi14"] = rsi(close)
    out = out.join(stochastic(df))
    out["atr14"] = atr(df)
    out["true_range"] = true_range(df)
    out = out.join(adx(df))
    out = out.join(bollinger(close))
    out = out.join(supertrend(df))
    out = out.join(ichimoku(df))
    out["roc10"] = close.pct_change(10) * 100
    body = (df["close"] - df["open"]).abs()
    out["avg_body20"] = body.rolling(20).mean()
    wick = (df["high"] - df["low"]) - body
    out["avg_wick20"] = wick.rolling(20).mean()
    out["rel_volume"] = df["volume"] / df["volume"].rolling(20).mean().replace(0, np.nan)
    out["volume_spike"] = out["rel_volume"] > 2.0
    if trading_days is not None:
        out["vwap"] = vwap_by_trading_day(df, trading_days)
    if "spread" in df.columns and df["spread"].notna().any():
        out["spread_pctile"] = df["spread"].rank(pct=True)
    return out


def latest_snapshot(indicators: pd.DataFrame) -> dict:
    """最後一根「已收線」列的指標快照(呼叫端需先過濾 is_closed)。"""
    if indicators.empty:
        return {}
    row = indicators.iloc[-1]
    return {k: (None if pd.isna(v) else (bool(v) if isinstance(v, (bool, np.bool_)) else round(float(v), 4)))
            for k, v in row.items()}

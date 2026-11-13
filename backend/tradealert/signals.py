"""Cheap technical signals computed from daily bars only (no network).

All functions take a DataFrame with Open/High/Low/Close/Volume columns, ascending
dates, whose LAST row is the scan day. Nothing here looks past the last row.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .utils import fnum


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0 or not math.isfinite(b):
        return None
    r = a / b
    return r if math.isfinite(r) else None


def _pct(a: float | None, b: float | None) -> float | None:
    r = _safe_div(a, b)
    return r - 1 if r is not None else None


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def bollinger_width(close: pd.Series, n: int = 20) -> pd.Series:
    ma = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return (4 * sd) / ma


def compute_signals(df: pd.DataFrame) -> dict:
    """Return a flat dict of raw signal values (floats/None/bools) for the last bar."""
    n = len(df)
    if n < 2:
        return {}
    o, h, l, c, v = (df[col].astype(float) for col in ("Open", "High", "Low", "Close", "Volume"))
    close = float(c.iloc[-1])
    prev_close = float(c.iloc[-2])
    vol = float(v.iloc[-1])

    # Volume vs. the 20 sessions BEFORE today (today excluded from its own baseline).
    prior_vol = v.iloc[-21:-1] if n >= 21 else v.iloc[:-1]
    avg_vol_20 = float(prior_vol.mean()) if len(prior_vol) else None
    rel_volume = _safe_div(vol, avg_vol_20)
    # Volume trend: last 3 sessions vs the 20 sessions before them.
    base = v.iloc[-23:-3] if n >= 23 else v.iloc[:-3]
    vol_trend_3d = _safe_div(float(v.iloc[-3:].mean()), float(base.mean()) if len(base) else None)
    rising_vol_days = int((v.iloc[-5:].diff().dropna() > 0).sum()) if n >= 5 else None

    def pct_back(k: int) -> float | None:
        if n <= k:
            return None
        return _pct(close, float(c.iloc[-1 - k]))

    dollar_volume = close * vol
    avg_dollar_volume_20 = float((c * v).iloc[-20:].mean())

    prior_high_20 = float(h.iloc[-21:-1].max()) if n >= 21 else float(h.iloc[:-1].max())
    breakout_20d = close > prior_high_20
    breakout_pct = _pct(close, prior_high_20)
    gap_pct = _pct(float(o.iloc[-1]), prev_close)
    day_range = float(h.iloc[-1] - l.iloc[-1])
    close_position = (close - float(l.iloc[-1])) / day_range if day_range > 0 else 0.5

    tr = true_range(df)
    atr14_prior = float(tr.iloc[-15:-1].mean()) if n >= 15 else None
    atr5_prior = float(tr.iloc[-6:-1].mean()) if n >= 6 else None
    range_expansion = _safe_div(float(tr.iloc[-1]), atr14_prior)
    atr_contraction = _safe_div(atr5_prior, atr14_prior)  # < 1 means ranges were contracting

    bbw = bollinger_width(c)
    bbw_prev = fnum(bbw.iloc[-2]) if n >= 22 else None
    bbw_hist = bbw.iloc[:-1].dropna()
    squeeze_ratio = _safe_div(bbw_prev, float(bbw_hist.median())) if len(bbw_hist) >= 10 else None
    bbw_pctile = (float((bbw_hist <= bbw_prev).mean()) if bbw_prev is not None and len(bbw_hist) >= 10 else None)

    period_high = float(h.max())
    period_low = float(l.min())

    # RSI(14), Wilder smoothing
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] > 0 else np.inf
    rsi14 = 100 - 100 / (1 + rs) if math.isfinite(rs) else 100.0

    up_days_5 = int((c.diff().iloc[-5:] > 0).sum()) if n >= 6 else None

    return {
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "close": close,
        "prev_close": prev_close,
        "open": float(o.iloc[-1]),
        "high": float(h.iloc[-1]),
        "low": float(l.iloc[-1]),
        "volume": vol,
        "avg_volume_20d": avg_vol_20,
        "rel_volume": rel_volume,
        "vol_trend_3d": vol_trend_3d,
        "rising_volume_days_4": rising_vol_days,
        "dollar_volume": dollar_volume,
        "avg_dollar_volume_20d": avg_dollar_volume_20,
        "pct_change_1d": pct_back(1),
        "pct_change_5d": pct_back(5),
        "pct_change_20d": pct_back(20),
        "prior_high_20d": prior_high_20,
        "breakout_20d": bool(breakout_20d),
        "breakout_pct": breakout_pct,
        "gap_pct": gap_pct,
        "close_position": close_position,
        "atr14_prior": atr14_prior,
        "range_expansion": range_expansion,
        "atr_contraction": atr_contraction,
        "bb_width_prev": bbw_prev,
        "squeeze_ratio": squeeze_ratio,
        "bb_width_percentile": bbw_pctile,
        "period_high": period_high,
        "period_low": period_low,
        "pct_from_period_high": _pct(close, period_high),
        "pct_from_period_low": _pct(close, period_low),
        "rsi14": float(rsi14),
        "up_days_5": up_days_5,
        "bars_count": n,
    }

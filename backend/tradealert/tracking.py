"""Forward-performance tracking of past picks (no look-ahead).

Entry = close on the scan's as-of trading day. Forward windows start on the NEXT
trading day: ret_Nd uses the close N bars after the scan day; max_gain_5d uses the
highest high and max_drawdown_5d the lowest low of the next 5 bars.
A window's value stays null until all of its bars exist.
"""
from __future__ import annotations

import datetime as dt
import statistics
from typing import Callable

import warnings

import numpy as np
import pandas as pd

from . import config, db
from .utils import fnum, is_incomplete_bar_date, log, now_et

VERDICTS = ("STRONG", "WATCH", "WEAK", "AVOID")


def compute_forward_outcome(bars: pd.DataFrame | None, scan_date: str, stored_entry: float | None) -> dict:
    """Compute forward returns for one pick from (split-adjusted) daily bars."""
    out = {"entry_close": stored_entry, "ret_1d": None, "ret_3d": None, "ret_5d": None, "ret_10d": None,
           "max_gain_5d": None, "max_drawdown_5d": None, "max_gain_10d": None, "max_drawdown_10d": None,
           "bars_available": 0, "complete": 0}
    if bars is None or bars.empty:
        return out
    day = pd.Timestamp(scan_date)
    # Re-read entry from the same (freshly adjusted) series when possible so later
    # splits/reverse splits don't create fake returns; fall back to the stored close.
    entry = stored_entry
    if day in bars.index:
        entry = fnum(bars.loc[day, "Close"]) or stored_entry
    if not entry or entry <= 0:
        return out
    fwd = bars[bars.index > day]
    n = len(fwd)
    out["entry_close"] = entry
    out["bars_available"] = n
    for w in config.FORWARD_WINDOWS:
        if n >= w:
            out[f"ret_{w}d"] = float(fwd["Close"].iloc[w - 1]) / entry - 1
    for w in (5, 10):
        if n >= w:
            win = fwd.iloc[:w]
            out[f"max_gain_{w}d"] = float(win["High"].max()) / entry - 1
            out[f"max_drawdown_{w}d"] = float(win["Low"].min()) / entry - 1
    out["complete"] = int(n >= max(config.FORWARD_WINDOWS))
    return out


# --------------------------------------------------------------------------- experiment windows
# One definition shared by forward experiments (scalar, below) and backtests (array form):
#   entry  = open of the first bar after the signal day ("next_open") or the signal-day close ("close")
#   window = the next `horizon` bars of the ticker, starting the bar after the signal day
#   return = close of the last window bar / entry - 1   (mark-to-market while partial)
#   max_gain = highest high in the window / entry - 1; max_drawdown = lowest low / entry - 1
#   hit = max_gain >= hit threshold, only once the full window exists (status "final")
ENTRY_MODES = ("next_open", "close")


def window_outcome(bars: pd.DataFrame | None, signal_date: str, horizon: int, entry: str = "next_open",
                   hit_threshold_pct: float | None = None, ref_close: float | None = None) -> dict:
    """Outcome of one pick over `horizon` bars after `signal_date` (split-adjusted bars)."""
    thr = (config.HIT_THRESHOLD_PCT if hit_threshold_pct is None else hit_threshold_pct) / 100.0
    out = {"entry_date": None, "entry_price": None, "exit_date": None, "exit_price": None, "ret": None,
           "max_gain": None, "max_drawdown": None, "hit": None, "status": "pending", "bars_available": 0}
    if bars is None or bars.empty:
        return out
    day = pd.Timestamp(signal_date)
    fwd = bars[bars.index > day]
    n = min(len(fwd), horizon)
    out["bars_available"] = n
    if entry == "close":
        # Re-read from the freshly adjusted series so later (reverse) splits don't fake a return.
        px = fnum(bars.loc[day, "Close"]) if day in bars.index else ref_close
        edate = day
    else:
        if fwd.empty:
            return out
        px, edate = fnum(fwd["Open"].iloc[0]), fwd.index[0]
    if not px or px <= 0:
        return out
    out["entry_price"], out["entry_date"] = px, edate.strftime("%Y-%m-%d")
    if n == 0:
        return out
    win = fwd.iloc[:n]
    out["exit_price"] = float(win["Close"].iloc[-1])
    out["exit_date"] = win.index[-1].strftime("%Y-%m-%d")
    out["ret"] = out["exit_price"] / px - 1
    out["max_gain"] = float(win["High"].max()) / px - 1
    out["max_drawdown"] = float(win["Low"].min()) / px - 1
    if n >= horizon:
        out["status"] = "final"
        out["hit"] = bool(out["max_gain"] >= thr)
    else:
        out["status"] = "partial"
    return out


def window_outcomes_array(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, horizon: int,
                          entry: str = "next_open") -> dict[str, np.ndarray]:
    """Vectorised `window_outcome` for every bar index of ONE ticker's own bar series.

    Returns arrays (len N): entry, exit, ret, max_gain, max_drawdown (NaN when unknown),
    n_avail (bars in the window so far). Index i is the signal day.
    """
    n_bars = len(c)
    idx = np.arange(n_bars)
    n_avail = np.clip(n_bars - 1 - idx, 0, horizon)
    pad = np.full(horizon + 1, np.nan)
    if entry == "close":
        ent = c.astype(float).copy()
    else:
        ent = np.r_[o[1:].astype(float), np.nan]
    ent = np.where(ent > 0, ent, np.nan)
    hp = np.r_[h.astype(float), pad]
    lp = np.r_[l.astype(float), pad]
    from numpy.lib.stride_tricks import sliding_window_view

    hw = sliding_window_view(hp[1:], horizon)[:n_bars]   # row i = h[i+1 : i+1+horizon]
    lw = sliding_window_view(lp[1:], horizon)[:n_bars]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mx = np.nanmax(hw, axis=1)
        mn = np.nanmin(lw, axis=1)
    exit_idx = idx + n_avail
    ext = np.where(n_avail > 0, c.astype(float)[np.minimum(exit_idx, n_bars - 1)], np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        return {
            "entry": ent, "exit": ext, "n_avail": n_avail, "exit_idx": exit_idx,
            "ret": ext / ent - 1, "max_gain": mx / ent - 1, "max_drawdown": mn / ent - 1,
        }


def last_settled_session(now: dt.datetime | None = None) -> dt.date:
    """Most recent weekday whose session has settled (holidays are not modelled)."""
    now = now or now_et()
    d = now.date()
    if is_incomplete_bar_date(d, now) or now.weekday() >= 5:
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def add_trading_days(d: dt.date, n: int) -> dt.date:
    """d + n weekdays (exchange holidays are not modelled)."""
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _default_fetch(tickers: list[str], start: str) -> dict[str, pd.DataFrame]:
    from .prices import download_bars

    return download_bars(tickers, start=start, use_cache=False, chunk_size=100)


def update_outcomes(
    db_path=None,
    fetch: Callable[[list[str], str], dict[str, pd.DataFrame]] | None = None,
    known_bars: dict[str, pd.DataFrame] | None = None,
) -> dict:
    """Fill/refresh forward outcomes for every pick whose 10-day window isn't complete yet.

    `known_bars` (e.g. the bars already downloaded by today's scan) are used when they
    cover the scan date; other tickers are fetched with `fetch`.
    """
    last_session = last_settled_session().isoformat()
    # Picks from the latest settled session cannot have forward bars yet: skip the fetch.
    pending = [p for p in db.picks_needing_outcomes(db_path) if p["scan_date"] < last_session]
    if not pending:
        return {"updated": 0, "pending": 0}
    fetch = fetch or _default_fetch
    known_bars = known_bars or {}
    by_ticker: dict[str, list[dict]] = {}
    for p in pending:
        by_ticker.setdefault(p["ticker"], []).append(p)
    earliest = min(p["scan_date"] for p in pending)
    need = []
    bars: dict[str, pd.DataFrame] = {}
    for t, picks in by_ticker.items():
        df = known_bars.get(t)
        oldest = min(p["scan_date"] for p in picks)
        if df is not None and len(df) and df.index[0] <= pd.Timestamp(oldest):
            bars[t] = df
        else:
            need.append(t)
    if need:
        start = (dt.date.fromisoformat(earliest) - dt.timedelta(days=7)).isoformat()
        try:
            bars.update(fetch(need, start))
        except Exception as exc:  # noqa: BLE001
            log.warning("tracking fetch failed: %s", exc)
    updated = 0
    for t, picks in by_ticker.items():
        for p in picks:
            res = compute_forward_outcome(bars.get(t), p["scan_date"], p["entry_close"])
            if res["bars_available"] or t in bars:
                db.upsert_outcome(p["candidate_id"], res, db_path)
                updated += 1
    return {"updated": updated, "pending": len(pending)}


def _avg(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def performance_report(db_path=None) -> dict:
    threshold = config.HIT_THRESHOLD_PCT / 100.0
    picks = db.all_picks_with_outcomes(db_path)
    by_verdict = []
    for v in VERDICTS:
        rows = [p for p in picks if p["verdict"] == v]
        done = [p for p in rows if p["ret_5d"] is not None and p["max_gain_5d"] is not None]
        r5 = [p["ret_5d"] for p in done]
        mg = [p["max_gain_5d"] for p in done]
        dd = [p["max_drawdown_5d"] for p in done if p["max_drawdown_5d"] is not None]
        by_verdict.append({
            "verdict": v,
            "count": len(done),
            "pending": len(rows) - len(done),
            "avg_close_return_5d": _avg(r5),
            "median_close_return_5d": statistics.median(r5) if r5 else None,
            "avg_max_gain_5d": _avg(mg),
            "hit_rate": (sum(1 for x in mg if x >= threshold) / len(mg)) if mg else None,
            "avg_max_drawdown_5d": _avg(dd),
        })
    return {
        "hit_threshold_pct": config.HIT_THRESHOLD_PCT,
        "by_verdict": by_verdict,
        "picks": [{
            "scan_date": p["scan_date"], "ticker": p["ticker"], "verdict": p["verdict"], "score": p["score"],
            "entry_close": p["entry_close"] if p["entry_close"] is not None else p["stored_close"],
            "ret_1d": p["ret_1d"], "ret_3d": p["ret_3d"], "ret_5d": p["ret_5d"], "ret_10d": p["ret_10d"],
            "max_gain_5d": p["max_gain_5d"], "max_drawdown_5d": p["max_drawdown_5d"],
        } for p in picks],
    }

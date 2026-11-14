"""Historical replay of the scanner ("backtest" experiments).

For each trading day D in a range, using ONLY bars up to and including D:
  1. the live price/liquidity filter as of D (close in band, 20-day avg dollar volume, >= MIN_BARS bars),
  2. the live technical signals, computed on the trailing LOOKBACK_BARS bars (the live scan
     downloads ~3 months = 65 bars, and some signals, e.g. the squeeze median, depend on that window),
  3. the live technical shortlist (top SHORTLIST_SIZE by technical score),
  4. for the shortlist, SEC filings replayed as of D (catalyst points + dilution/risk flags),
  5. the score rescaled to 0-100 over the components that can be reconstructed, normal verdicts,
  6. the top N by score (>= min_score) become the day's picks.
Every filtered stock of the day is also evaluated as the baseline.

Signals are vectorised per ticker over its own bar series (no per-day recomputation), so a
6-12 month replay over ~1,000+ filtered stocks/day takes well under a few minutes once
the bars are downloaded. Outcomes use `tracking.window_outcomes_array` (same definition as
forward experiments).
"""
from __future__ import annotations

import datetime as dt
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import numpy as np
import pandas as pd

from . import config, scoring, tracking
from .utils import log

LOOKBACK_BARS = 65  # bars in the live scan's 3-month download
WARMUP_CALENDAR_DAYS = 100  # calendar days of history needed before the first signal day
LONG_PERIOD = "2y"

Progress = Callable[[float, str], None]
# filings provider: (tickers, needed_through) -> {ticker: history list | None (no SEC filer)}
FilingsProvider = Callable[[list[str], dt.date], dict]

SIGNAL_COLUMNS = [
    "close", "open", "volume", "rel_volume", "vol_trend_3d", "pct_change_1d", "pct_change_5d",
    "breakout_20d", "breakout_pct", "gap_pct", "close_position", "range_expansion", "squeeze_ratio",
    "avg_dollar_volume_20d", "bars_count",
]


def _div(a: pd.Series, b: pd.Series) -> pd.Series:
    """a / b with the live `_safe_div` semantics: NaN when b is 0 or the result is not finite."""
    with np.errstate(divide="ignore", invalid="ignore"):
        r = a / b.where(b != 0)
    return r.where(np.isfinite(r))


def ticker_signals(df: pd.DataFrame, lookback: int = LOOKBACK_BARS) -> pd.DataFrame:
    """Signals for EVERY bar of one ticker, each computed only from bars up to that bar.

    Row i equals `signals.compute_signals(df.iloc[i-lookback+1 : i+1])` for the keys the
    score uses (verified in tests). All operations are backward-looking rolling windows.
    """
    o, h, l, c, v = (df[k].astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    prev_c = c.shift(1)
    out = pd.DataFrame(index=df.index)
    out["close"], out["open"], out["volume"] = c, o, v
    out["rel_volume"] = _div(v, v.rolling(20).mean().shift(1))
    out["vol_trend_3d"] = _div(v.rolling(3).mean(), v.rolling(20).mean().shift(3))
    out["pct_change_1d"] = _div(c, prev_c) - 1
    out["pct_change_5d"] = _div(c, c.shift(5)) - 1
    prior_high = h.rolling(20).max().shift(1)
    out["breakout_20d"] = c > prior_high
    out["breakout_pct"] = _div(c, prior_high) - 1
    out["gap_pct"] = _div(o, prev_c) - 1
    rng = h - l
    out["close_position"] = np.where(rng > 0, (c - l) / rng.where(rng > 0), 0.5)
    tr = pd.concat([rng, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["range_expansion"] = _div(tr, tr.rolling(14).mean().shift(1))
    ma, sd = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    bbw = _div(4 * sd, ma)
    bbw_med = bbw.rolling(lookback - 20, min_periods=10).median().shift(1)
    out["squeeze_ratio"] = _div(bbw.shift(1), bbw_med)
    out["avg_dollar_volume_20d"] = (c * v).rolling(20, min_periods=1).mean()
    out["bars_count"] = np.minimum(np.arange(1, len(df) + 1), lookback)
    return out


def _signal_dict(row: dict) -> dict:
    s = {}
    for k in SIGNAL_COLUMNS:
        val = row.get(k)
        if isinstance(val, (bool, np.bool_)):
            s[k] = bool(val)
        elif val is None or (isinstance(val, float) and not math.isfinite(val)):
            s[k] = None
        else:
            s[k] = float(val)
    return s


def market_calendar(bars: dict[str, pd.DataFrame], min_share: float = 0.2) -> pd.DatetimeIndex:
    """Dates on which a meaningful share of tickers have a bar (drops stray holiday prints)."""
    counts = pd.Series(np.concatenate([df.index.values for df in bars.values() if df is not None and len(df)]))
    vc = counts.value_counts()
    if vc.empty:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(sorted(vc[vc >= vc.max() * min_share].index))


def build_rows(bars: dict[str, pd.DataFrame], start: dt.date, end: dt.date, horizon: int, entry: str,
               progress: Progress | None = None) -> pd.DataFrame:
    """One row per (signal day, ticker) passing the live filter as of that day, with outcomes."""
    cal = market_calendar(bars)
    days = cal[(cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))]
    day_set = set(days)
    lo, hi = config.MIN_PRICE, config.MAX_PRICE
    frames = []
    items = list(bars.items())
    t0 = time.monotonic()
    for k, (t, df) in enumerate(items, 1):
        if df is None or len(df) < config.MIN_BARS:
            continue
        idx = df.index
        in_range = (idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))
        if not in_range.any():
            continue
        sig = ticker_signals(df)
        mask = (in_range & idx.isin(day_set) & (sig["bars_count"].values >= config.MIN_BARS)
                & (sig["close"].values >= lo) & (sig["close"].values <= hi)
                & (sig["avg_dollar_volume_20d"].values >= config.MIN_AVG_DOLLAR_VOLUME))
        if not mask.any():
            continue
        oc = tracking.window_outcomes_array(df["Open"].values, df["High"].values, df["Low"].values,
                                            df["Close"].values, horizon, entry)
        part = sig[mask].copy()
        part["ticker"] = t
        part["signal_date"] = idx[mask]
        ent_idx = np.flatnonzero(mask) + (0 if entry == "close" else 1)
        dates = np.r_[idx.values, np.datetime64("NaT", "ns")]
        part["entry_date"] = dates[np.minimum(ent_idx, len(idx))]
        part["exit_date"] = dates[np.where(oc["n_avail"][mask] > 0, oc["exit_idx"][mask], len(idx))]
        for key in ("entry", "exit", "ret", "max_gain", "max_drawdown", "n_avail"):
            part[key] = oc[key][mask]
        frames.append(part.reset_index(drop=True))
        if progress and k % 250 == 0:
            progress(k / len(items), f"Signals {k}/{len(items)} tickers ({time.monotonic() - t0:.0f}s)")
    if not frames:
        return pd.DataFrame(columns=SIGNAL_COLUMNS + ["ticker", "signal_date", "entry", "exit", "ret", "max_gain",
                                                     "max_drawdown", "n_avail", "entry_date", "exit_date"])
    rows = pd.concat(frames, ignore_index=True)
    return rows


def outcome_status(rows: pd.DataFrame, horizon: int, hit_threshold_pct: float) -> pd.DataFrame:
    ok = np.isfinite(rows["entry"].astype(float).values)
    n = rows["n_avail"].values
    status = np.where(~ok | (n <= 0), "pending", np.where(n >= horizon, "final", "partial"))
    rows["status"] = status
    hit = np.where(status == "final", (rows["max_gain"].values >= hit_threshold_pct / 100.0).astype(float), np.nan)
    rows["hit"] = hit
    for col in ("ret", "max_gain", "max_drawdown"):
        rows.loc[status == "pending", col] = np.nan
    return rows


def default_filings_provider(progress: Progress | None = None) -> FilingsProvider | None:
    """SEC history provider, or None if SEC is not configured/reachable."""
    if not config.sec_user_agent():
        return None
    from . import filings

    ticker_map = filings.load_ticker_map()

    def provider(tickers: list[str], needed_through: dt.date) -> dict:
        out: dict[str, list | None] = {}
        todo = []
        for t in tickers:
            info = ticker_map.get(t.upper())
            if info is None:
                out[t] = None
            else:
                todo.append((t, info["cik"]))
        errors = 0

        def one(item):
            t, cik = item
            try:
                return t, filings.fetch_filing_history(cik, needed_through), None
            except Exception as exc:  # noqa: BLE001
                return t, None, str(exc)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for i, (t, hist, err) in enumerate(pool.map(one, todo), 1):
                if err:
                    errors += 1
                    log.warning("EDGAR history failed for %s: %s", t, err)
                    out[t] = {"error": err}
                else:
                    out[t] = hist
                if progress and (i % 25 == 0 or i == len(todo)):
                    progress(i / max(1, len(todo)), f"SEC filings {i}/{len(todo)} tickers")
        return out

    return provider


def run_backtest(bars: dict[str, pd.DataFrame], params: dict, names: dict[str, str] | None = None,
                 filings_provider: FilingsProvider | None = None,
                 progress: Progress | None = None) -> dict:
    """Pure replay on given bars (no price download). Returns {picks, baseline, notes, stats}.

    `picks` are dict rows for experiment_picks; `baseline` is a DataFrame with
    signal_date/ret/max_gain/max_drawdown/hit/status for every filtered stock-day.
    """
    names = names or {}
    prog = progress or (lambda f, m: None)
    start, end = dt.date.fromisoformat(params["start_date"]), dt.date.fromisoformat(params["end_date"])
    horizon, entry = int(params["horizon_days"]), params["entry"]
    thr = float(params["hit_threshold_pct"])
    top_n, min_score = int(params["top_n"]), float(params["min_score"])
    t0 = time.monotonic()

    rows = build_rows(bars, start, end, horizon, entry, progress=lambda f, m: prog(0.0 + 0.45 * f, m))
    rows = outcome_status(rows, horizon, thr)
    n_days = rows["signal_date"].nunique() if len(rows) else 0
    prog(0.45, f"{len(rows)} filtered stock-days over {n_days} trading days ({time.monotonic() - t0:.0f}s)")

    # Technical components for every filtered stock-day (same functions as the live scan).
    recs = rows[SIGNAL_COLUMNS].to_dict("records")
    vol_p, mom_p, set_p = np.empty(len(recs)), np.empty(len(recs)), np.empty(len(recs))
    for i, r in enumerate(recs):
        s = _signal_dict(r)
        vol_p[i] = scoring.volume_component(s)["points"]
        mom_p[i] = scoring.momentum_component(s)["points"]
        set_p[i] = scoring.setup_component(s)["points"]
        if i and i % 20000 == 0:
            prog(0.45 + 0.15 * i / len(recs), f"Technical scores {i}/{len(recs)}")
    rows["technical_score"] = vol_p + mom_p + set_p
    rows["_vol"], rows["_mom"], rows["_set"] = vol_p, mom_p, set_p
    prog(0.60, f"Technical scores done ({time.monotonic() - t0:.0f}s)")

    # Live shortlist per day: top SHORTLIST_SIZE by technical score.
    k = max(config.SHORTLIST_SIZE, top_n)
    rows = rows.sort_values(["signal_date", "technical_score", "ticker"], ascending=[True, False, True])
    rows["_rank"] = rows.groupby("signal_date").cumcount()
    short = rows[rows["_rank"] < k].copy()

    # Filings replay for the shortlisted tickers.
    include_filings = bool(params.get("include_filings", True))
    histories: dict = {}
    filings_note = None
    if include_filings and filings_provider is not None:
        tickers = sorted(short["ticker"].unique())
        prog(0.60, f"Fetching SEC filing history for {len(tickers)} shortlisted tickers")
        histories = filings_provider(tickers, end)
        n_cik = sum(1 for t in tickers if isinstance(histories.get(t), dict) and "filings" in histories[t])
        n_none = sum(1 for t in tickers if histories.get(t) is None)
        n_err = sum(1 for t in tickers if isinstance(histories.get(t), dict) and "error" in histories[t])
        thin = sum(1 for t in tickers if isinstance(histories.get(t), dict) and histories[t].get("oldest")
                   and histories[t].get("has_more_files")
                   and histories[t]["oldest"] > (start - dt.timedelta(days=config.FILINGS_LOOKBACK_DAYS)).isoformat())
        filings_note = (f"SEC filings replayed as of each day for {n_cik} of {len(tickers)} shortlisted tickers "
                        f"({n_none} had no SEC filer -> NO_SEC_DATA flag, {n_err} fetch errors"
                        + (f", {thin} with older filings outside EDGAR's 'recent' block" if thin else "") + "). "
                        "A filing counts on day D only if EDGAR accepted it by 16:30 ET on D.")
    elif include_filings:
        include_filings = False
        filings_note = ("SEC filings were NOT replayed (SEC_USER_AGENT not set or SEC unreachable): "
                        "no catalyst points and no dilution/AVOID flags in this backtest.")
    else:
        filings_note = "SEC filings replay disabled by parameter (include_filings = false)."

    from . import filings as filings_mod

    cat_max = 0.5 * config.WEIGHTS["catalyst"] if include_filings else 0.0
    avail_max = config.WEIGHTS["volume"] + config.WEIGHTS["momentum"] + config.WEIGHTS["setup"] + cat_max
    scale = 100.0 / avail_max
    scores, verdicts, flag_codes, details = [], [], [], []
    recs = short[SIGNAL_COLUMNS + ["signal_date", "ticker", "_vol", "_mom", "_set"]].to_dict("records")
    for i, r in enumerate(recs):
        s = _signal_dict(r)
        day = pd.Timestamp(r["signal_date"]).date()
        cat_pts, fil_flags, extra_flags, cat_reason = 0.0, [], [], None
        if include_filings:
            h = histories.get(r["ticker"])
            if h is None:
                extra_flags.append({"code": "NO_SEC_DATA", "severity": "low",
                                    "text": "No SEC filer found for this ticker: filings/dilution could not be checked"})
            elif "filings" in h:
                fs = filings_mod.filings_as_of(h["filings"], day)
                cat = scoring.catalyst_component(fs, 0, 0)
                cat_pts, cat_reason, fil_flags = cat["points"], cat["reason"], fs["risk_flags"]
        flags = scoring.merge_flags(scoring.technical_risk_flags(s), fil_flags, extra_flags)
        pen = scoring.penalty_component(flags)
        raw = r["_vol"] + r["_mom"] + r["_set"] + cat_pts
        score = round(max(0.0, min(100.0, raw * scale + pen["points"])), 1)
        scores.append(score)
        verdicts.append(scoring.verdict_for(score, flags))
        flag_codes.append([f["code"] for f in flags])
        details.append({
            "components": {"volume": r["_vol"], "momentum": r["_mom"], "setup": r["_set"],
                           **({"catalyst_filings": cat_pts} if include_filings else {}),
                           "risk_penalty": pen["points"]},
            "raw_points": round(raw, 2), "available_max": avail_max,
            "risk_flags": flags, "catalyst_reason": cat_reason,
            "signals": {k2: s[k2] for k2 in ("close", "rel_volume", "pct_change_1d", "pct_change_5d",
                                              "breakout_20d", "squeeze_ratio", "range_expansion", "gap_pct",
                                              "avg_dollar_volume_20d")},
        })
    short["score"], short["verdict"] = scores, verdicts
    short["_flags"], short["_details"] = flag_codes, details
    prog(0.90, f"Scored {len(short)} shortlisted stock-days ({time.monotonic() - t0:.0f}s)")

    picks_df = short[short["score"] >= min_score].sort_values(
        ["signal_date", "score", "ticker"], ascending=[True, False, True])
    picks_df = picks_df.groupby("signal_date", group_keys=False).head(top_n)

    def _d(x):
        return None if pd.isna(x) else pd.Timestamp(x).strftime("%Y-%m-%d")

    def _f(x):
        return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)

    picks = []
    for r in picks_df.to_dict("records"):
        picks.append({
            "signal_date": _d(r["signal_date"]), "ticker": r["ticker"], "name": names.get(r["ticker"]),
            "score": r["score"], "verdict": r["verdict"], "risk_flags": r["_flags"], "details": r["_details"],
            "ref_close": _f(r["close"]), "entry_date": _d(r["entry_date"]) if r["status"] != "pending" else None,
            "entry_price": _f(r["entry"]) if r["status"] != "pending" else None,
            "exit_date": _d(r["exit_date"]) if r["status"] != "pending" else None,
            "exit_price": _f(r["exit"]) if r["status"] != "pending" else None,
            "ret": _f(r["ret"]), "max_gain": _f(r["max_gain"]), "max_drawdown": _f(r["max_drawdown"]),
            "hit": None if pd.isna(r["hit"]) else bool(r["hit"]), "status": r["status"],
            "bars_available": int(r["n_avail"]),
        })
    baseline = rows[["signal_date", "ticker", "ret", "max_gain", "max_drawdown", "hit", "status"]].copy()
    baseline["signal_date"] = baseline["signal_date"].dt.strftime("%Y-%m-%d")
    used = ["volume (25)", "momentum (25)", "setup (15)"] + (["catalyst, SEC-filing half only (10 of 20)"]
                                                              if include_filings else [])
    stats = {
        "days": int(n_days), "filtered_stock_days": int(len(rows)),
        "avg_filtered_per_day": round(len(rows) / n_days, 1) if n_days else 0,
        "shortlist_size": k, "available_max_points": avail_max, "components_used": used,
        "filings_replayed": include_filings, "seconds": round(time.monotonic() - t0, 1),
    }
    notes = [
        f"Score components used: {', '.join(used)}; the raw points (max {avail_max:g}) are rescaled to 0-100, "
        "then the normal risk penalties and verdict thresholds (STRONG >= "
        f"{config.VERDICT_STRONG}, WATCH >= {config.VERDICT_WATCH}, any high-severity flag = AVOID) apply. "
        "Not available historically: float/short interest (15 pts), news (half of catalyst), NASDAQ "
        "financial-status, reverse-split and market-cap flags.",
        filings_note,
        f"Each day: live price/liquidity filter as of that day, signals on the trailing {LOOKBACK_BARS} bars "
        f"(the live 3-month window), live shortlist of the top {k} by technical score, then the top {top_n} "
        f"by final score >= {min_score:g} become picks. Baseline = every stock that passed the filter that day "
        "(picks included), evaluated the same way.",
        "Survivorship bias: only symbols listed TODAY are replayed. Stocks that were delisted, went bankrupt "
        "or were acquired during the period are missing, which flatters both picks and baseline (more so "
        "for penny stocks, where delistings are common).",
        "Prices are split-adjusted as of today: a stock that later did a 1:10 reverse split shows 10x its real "
        "historical price, so it may wrongly fail the $0.10-$5 filter (and forward splits the opposite). "
        "Which stocks looked like 'penny' stocks historically is therefore approximate.",
        "No historical float, short interest or news: the replayed score is a technical (+ filings) score, "
        "not the full live score.",
        ("Entry at the next day's open assumes you get that price. For illiquid penny stocks the real fill is "
         "often worse (spread, gaps, slippage); no commissions or slippage are modelled, so returns are "
         "optimistic.") if entry == "next_open" else
        ("Entry at the signal-day close assumes you could buy at the close you were screening on, which is "
         "not achievable after the close; returns are optimistic."),
        f"Window = the ticker's own next {horizon} bars (halted days are skipped); max gain uses intraday highs, "
        "and bad prints in free Yahoo data can inflate it. Hit = max gain >= "
        f"{thr:g}% within the window.",
    ]
    prog(1.0, f"Backtest computed in {time.monotonic() - t0:.0f}s")
    return {"picks": picks, "baseline": baseline, "notes": notes, "stats": stats}

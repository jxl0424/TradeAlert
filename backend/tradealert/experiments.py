"""Experiments: test whether the tool's recommendations are any good.

* backtest: replay the scanner over a past date range (see `backtest.py`), compare the
  picks with a baseline of every stock that passed the filter on the same days.
* forward: freeze the picks of a completed scan now, then evaluate them automatically
  (end of every scan, server startup, explicit endpoint) as the next bars arrive.

Every experiment is stored permanently in the `experiments` / `experiment_picks` tables.
"""
from __future__ import annotations

import datetime as dt
import math
import threading
import time
from typing import Callable

import pandas as pd

from . import config, db, tracking
from .utils import log, now_et, utc_now_iso

KINDS = ("backtest", "forward")
VERDICTS = tracking.VERDICTS
PICKS_CAP = 2000
COMPLETE_GRACE_DAYS = 3  # trading days after due_date before missing bars stop blocking completion

RANGES = {
    "horizon_days": {"min": 1, "max": 20},
    "hit_threshold_pct": {"min": 1, "max": 500},
    "top_n": {"min": 1, "max": 50},
    "min_score": {"min": 0, "max": 100},
}


class ParamError(ValueError):
    """Bad experiment parameters (HTTP 400)."""


class Conflict(RuntimeError):
    """Another experiment or a scan is computing (HTTP 409)."""


# --------------------------------------------------------------------------- defaults / validation
def _today() -> dt.date:
    return now_et().date()


def backtest_date_bounds(horizon: int = 5) -> dict:
    """Allowed and default backtest dates given the 2-year download and the warm-up."""
    from .backtest import WARMUP_CALENDAR_DAYS

    last = tracking.last_settled_session()
    earliest = _today() - dt.timedelta(days=730 - WARMUP_CALENDAR_DAYS)
    # far enough back that every window (next_open entry + horizon bars) has completed
    default_end = last
    for _ in range(horizon + 1):
        default_end -= dt.timedelta(days=1)
        while default_end.weekday() >= 5:
            default_end -= dt.timedelta(days=1)
    default_start = (pd.Timestamp(default_end) - pd.DateOffset(months=6) + pd.Timedelta(days=1)).date()
    return {"earliest_start": earliest, "latest_end": last, "default_start": default_start,
            "default_end": default_end}


def defaults() -> dict:
    b = backtest_date_bounds(5)
    return {
        "backtest": {
            "params": {
                "start_date": b["default_start"].isoformat(), "end_date": b["default_end"].isoformat(),
                "horizon_days": 5, "hit_threshold_pct": config.HIT_THRESHOLD_PCT, "top_n": 10, "min_score": 0,
                "entry": "next_open", "include_filings": True,
            },
            "ranges": {
                "start_date": {"min": b["earliest_start"].isoformat(), "max": b["latest_end"].isoformat()},
                "end_date": {"min": b["earliest_start"].isoformat(), "max": b["latest_end"].isoformat()},
                **RANGES,
                "entry": {"options": list(tracking.ENTRY_MODES)},
                "include_filings": {"type": "boolean"},
            },
            "sec_configured": bool(config.sec_user_agent()),
        },
        "forward": {
            "params": {
                "scan_id": None, "run_fresh_scan": False, "verdicts": ["STRONG", "WATCH"], "min_score": 0,
                "horizon_days": 5, "hit_threshold_pct": config.HIT_THRESHOLD_PCT, "entry": "next_open",
            },
            "ranges": {
                "horizon_days": RANGES["horizon_days"], "hit_threshold_pct": RANGES["hit_threshold_pct"],
                "min_score": RANGES["min_score"], "verdicts": {"options": list(VERDICTS)},
                "entry": {"options": list(tracking.ENTRY_MODES)},
                "scan_id": {"type": "integer", "nullable": True}, "run_fresh_scan": {"type": "boolean"},
            },
        },
    }


def _num(params: dict, key: str, default, integer: bool = False):
    v = params.get(key, default)
    if v is None:
        v = default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
        raise ParamError(f"{key} must be a number")
    if integer and float(v) != int(v):
        raise ParamError(f"{key} must be an integer")
    r = RANGES[key]
    if not (r["min"] <= v <= r["max"]):
        raise ParamError(f"{key} must be between {r['min']} and {r['max']}")
    return int(v) if integer else float(v)


def _bool(params: dict, key: str, default: bool) -> bool:
    v = params.get(key, default)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ParamError(f"{key} must be true or false")
    return v


def _entry(params: dict) -> str:
    v = params.get("entry") or "next_open"
    if v not in tracking.ENTRY_MODES:
        raise ParamError(f"entry must be one of {', '.join(tracking.ENTRY_MODES)}")
    return v


def _date(params: dict, key: str, default: dt.date) -> dt.date:
    v = params.get(key)
    if v in (None, ""):
        return default
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ParamError(f"{key} must be a date YYYY-MM-DD") from None


def validate_params(kind: str, params: dict | None, db_path=None) -> dict:
    """Fill defaults and validate. Raises ParamError."""
    if kind not in KINDS:
        raise ParamError(f"kind must be one of {', '.join(KINDS)}")
    if params is not None and not isinstance(params, dict):
        raise ParamError("params must be an object")
    params = dict(params or {})
    allowed = set(defaults()[kind]["params"])
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ParamError(f"unknown parameter(s) for {kind}: {', '.join(unknown)}")
    horizon = _num(params, "horizon_days", 5, integer=True)
    hit = _num(params, "hit_threshold_pct", config.HIT_THRESHOLD_PCT)
    min_score = _num(params, "min_score", 0)
    entry = _entry(params)
    if kind == "backtest":
        b = backtest_date_bounds(horizon)
        start = _date(params, "start_date", b["default_start"])
        end = _date(params, "end_date", b["default_end"])
        if start > end:
            raise ParamError("start_date must be on or before end_date")
        if start < b["earliest_start"]:
            raise ParamError(f"start_date must be on or after {b['earliest_start']} (2 years of history "
                             "minus the signal warm-up)")
        if end > b["latest_end"]:
            raise ParamError(f"end_date must be on or before {b['latest_end']} (last settled session)")
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "horizon_days": horizon,
                "hit_threshold_pct": hit, "top_n": _num(params, "top_n", 10, integer=True),
                "min_score": min_score, "entry": entry,
                "include_filings": _bool(params, "include_filings", True)}
    # forward
    fresh = _bool(params, "run_fresh_scan", False)
    scan_id = params.get("scan_id")
    if scan_id is not None and (isinstance(scan_id, bool) or not isinstance(scan_id, int)):
        raise ParamError("scan_id must be an integer")
    if fresh and scan_id is not None:
        raise ParamError("use either scan_id or run_fresh_scan, not both")
    verdicts = params.get("verdicts") or ["STRONG", "WATCH"]
    if not isinstance(verdicts, list) or not all(isinstance(v, str) for v in verdicts):
        raise ParamError("verdicts must be a list of strings")
    verdicts = [v.upper() for v in verdicts]
    bad = [v for v in verdicts if v not in VERDICTS]
    if bad:
        raise ParamError(f"unknown verdict(s): {', '.join(bad)}")
    if not fresh:
        s = db.get_scan(scan_id, db_path) if scan_id is not None else db.latest_completed_scan(db_path)
        if s is None:
            raise ParamError(f"scan {scan_id} not found" if scan_id is not None
                             else "no completed scan yet: set run_fresh_scan to true")
        if s["status"] != "completed" or not s.get("scan_date"):
            raise ParamError(f"scan {s['id']} is not completed")
        scan_id = s["id"]
    return {"scan_id": scan_id, "run_fresh_scan": fresh, "verdicts": verdicts, "min_score": min_score,
            "horizon_days": horizon, "hit_threshold_pct": hit, "entry": entry}


# --------------------------------------------------------------------------- runner state
class Runner:
    """In-process 'one experiment computing at a time' state, with DB-persisted progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.exp_id: int | None = None

    def try_start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running, self.exp_id = True, None
            return True

    def finish(self) -> None:
        with self._lock:
            self.running, self.exp_id = False, None


RUNNER = Runner()
EVAL_LOCK = threading.Lock()


def _reporter(exp_id: int, db_path, lo: float = 0.0, hi: float = 1.0) -> Callable[[float, str], None]:
    last = [0.0]

    def report(frac: float, msg: str) -> None:
        now = time.monotonic()
        p = lo + (hi - lo) * max(0.0, min(1.0, frac))
        if now - last[0] >= 1.0 or frac >= 1.0:
            last[0] = now
            db.update_experiment(exp_id, db_path, progress=round(p, 4), message=msg)
            log.info("[experiment %s %.0f%%] %s", exp_id, p * 100, msg)

    return report


def busy_reason(db_path=None) -> str | None:
    from . import scan

    if RUNNER.running:
        return "another experiment is computing"
    if scan.STATE.running:
        return "a scan is running"
    other = db.fresh_running_experiment(db_path)
    if other is not None:
        return f"experiment {other['id']} is computing"
    return None


def _default_name(kind: str, params: dict, db_path=None) -> str:
    if kind == "backtest":
        return f"Backtest {params['start_date']} to {params['end_date']}"
    if params.get("run_fresh_scan"):
        return "Forward test from a fresh scan"
    s = db.get_scan(params["scan_id"], db_path)
    return f"Forward test from scan #{params['scan_id']} ({s['scan_date'] if s else '?'})"


def start(kind: str, params: dict | None, name: str | None = None, db_path=None,
          background: bool = True) -> int:
    """Validate, create the experiment row, run it (thread or inline). Returns its id.

    Raises ParamError (400) or Conflict (409).
    """
    db.init_db(db_path)
    clean_params = validate_params(kind, params, db_path)
    if name is not None and not isinstance(name, str):
        raise ParamError("name must be a string")
    reason = busy_reason(db_path)
    if reason or not RUNNER.try_start():
        raise Conflict(reason or "another experiment is computing")
    try:
        exp_id = db.create_experiment(kind, (name or "").strip() or _default_name(kind, clean_params, db_path),
                                      clean_params, db_path)
        RUNNER.exp_id = exp_id
    except Exception:
        RUNNER.finish()
        raise
    if not background:
        try:
            run(exp_id, db_path)
        finally:
            RUNNER.finish()
        return exp_id

    def target():
        try:
            run(exp_id, db_path)
        finally:
            RUNNER.finish()

    threading.Thread(target=target, name=f"tradealert-experiment-{exp_id}", daemon=True).start()
    return exp_id


def run(exp_id: int, db_path=None) -> None:
    exp = db.get_experiment(exp_id, db_path)
    try:
        if exp["kind"] == "backtest":
            run_backtest_experiment(exp_id, db_path)
        else:
            run_forward_experiment(exp_id, db_path)
    except Exception as exc:  # noqa: BLE001 - recorded on the experiment
        log.exception("experiment %s failed", exp_id)
        db.update_experiment(exp_id, db_path, status="failed", finished_at=utc_now_iso(),
                             error=f"{type(exc).__name__}: {exc}", message=f"Failed: {exc}", progress=None)


# --------------------------------------------------------------------------- summaries
def group_stats(df: pd.DataFrame) -> dict:
    """G = {count, evaluated, partial, basis, hit_rate, avg_return, median_return, avg_max_gain,
    avg_max_drawdown, win_rate}. Return figures use final rows; if none are final yet they use
    partial (mark-to-market) rows and `basis` says so. hit_rate only counts final rows."""
    def f(x):
        return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)

    n = int(len(df))
    if n == 0:
        return {"count": 0, "evaluated": 0, "partial": 0, "basis": None, "hit_rate": None, "avg_return": None,
                "median_return": None, "avg_max_gain": None, "avg_max_drawdown": None, "win_rate": None}
    st = df["status"].values
    final, partial = df[st == "final"], df[st == "partial"]
    basis_name = "final" if len(final) else ("partial" if len(partial) else None)
    basis = final if len(final) else partial
    ret = basis["ret"].astype(float).dropna()
    hits = final["hit"].astype(float).dropna()
    return {
        "count": n, "evaluated": int(len(final)), "partial": int(len(partial)), "basis": basis_name,
        "hit_rate": f(hits.mean()) if len(hits) else None,
        "avg_return": f(ret.mean()) if len(ret) else None,
        "median_return": f(ret.median()) if len(ret) else None,
        "avg_max_gain": f(basis["max_gain"].astype(float).mean()) if len(basis) else None,
        "avg_max_drawdown": f(basis["max_drawdown"].astype(float).mean()) if len(basis) else None,
        "win_rate": f((ret > 0).mean()) if len(ret) else None,
    }


def _frame(rows) -> pd.DataFrame:
    cols = ["signal_date", "verdict", "ret", "max_gain", "max_drawdown", "hit", "status"]
    if isinstance(rows, pd.DataFrame):
        df = rows.copy()
    else:
        df = pd.DataFrame(list(rows))
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols].copy()
    for c in ("ret", "max_gain", "max_drawdown", "hit"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def compute_summary(picks, baseline, horizon: int, hit_threshold_pct: float) -> dict:
    p, b = _frame(picks), _frame(baseline)
    overall, base = group_stats(p), group_stats(b)

    def with_baseline(g: dict, bsub: pd.DataFrame) -> dict:
        bg = group_stats(bsub)
        g["baseline_hit_rate"], g["baseline_avg_return"] = bg["hit_rate"], bg["avg_return"]
        g["baseline_count"] = bg["count"]
        return g

    by_verdict = []
    for v in VERDICTS:
        sub = p[p["verdict"] == v]
        days = set(sub["signal_date"])
        by_verdict.append(with_baseline({**group_stats(sub), "verdict": v}, b[b["signal_date"].isin(days)]))
    by_month = []
    months = sorted(set(str(d)[:7] for d in p["signal_date"].dropna()))
    for m in months:
        sub = p[p["signal_date"].astype(str).str[:7] == m]
        by_month.append(with_baseline({**group_stats(sub), "month": m},
                                      b[b["signal_date"].astype(str).str[:7] == m]))
    # Equity curve: each day, 1/horizon of capital goes equally into that day's picks
    # (or the whole baseline) for `horizon` bars -> growth *= 1 + avg_return / horizon.
    basis = "final" if overall["evaluated"] or base["evaluated"] else "partial"
    pe = p[p["status"] == basis].groupby("signal_date")["ret"].mean()
    be = b[b["status"] == basis].groupby("signal_date")["ret"].mean()
    curve, gp, gb = [], 1.0, 1.0
    for d in sorted(set(pe.index) | set(be.index)):
        pr = pe.get(d)
        br = be.get(d)
        pr = None if pr is None or not math.isfinite(pr) else float(pr)
        br = None if br is None or not math.isfinite(br) else float(br)
        if pr is not None:
            gp *= 1 + pr / horizon
        if br is not None:
            gb *= 1 + br / horizon
        curve.append({"date": d, "picks_avg_return": pr, "baseline_avg_return": br,
                      "cumulative_picks": gp - 1, "cumulative_baseline": gb - 1})
    return {"overall": overall, "baseline": base, "by_verdict": by_verdict, "by_month": by_month,
            "equity_curve": curve, "horizon_days": horizon, "hit_threshold_pct": hit_threshold_pct,
            "edge": {
                "hit_rate": (overall["hit_rate"] - base["hit_rate"])
                if overall["hit_rate"] is not None and base["hit_rate"] is not None else None,
                "avg_return": (overall["avg_return"] - base["avg_return"])
                if overall["avg_return"] is not None and base["avg_return"] is not None else None,
            }}


# --------------------------------------------------------------------------- backtest
def run_backtest_experiment(exp_id: int, db_path=None, bars: dict | None = None,
                            filings_provider=None, names: dict | None = None) -> None:
    from . import backtest, prices, universe

    exp = db.get_experiment(exp_id, db_path)
    params = exp["params"]
    t0 = time.monotonic()
    rep = _reporter(exp_id, db_path)
    if bars is None:
        rep(0.0, "Loading symbol universe")
        secs = universe.load_universe()
        names = {s.ticker: s.name for s in secs}

        def on_chunk(done, total):
            rep(0.02 + 0.53 * done / total, f"Long-history price chunks {done}/{total} ({len(secs)} symbols, "
                                            f"{backtest.LONG_PERIOD})")

        bars = prices.download_long_history([s.ticker for s in secs], period=backtest.LONG_PERIOD,
                                            progress=on_chunk)
        if not bars:
            raise RuntimeError("no price data downloaded (network or Yahoo rate limit?)")
        dl_seconds = time.monotonic() - t0
    else:
        dl_seconds = 0.0
    if filings_provider is None and params.get("include_filings"):
        try:
            filings_provider = backtest.default_filings_provider(_reporter(exp_id, db_path, 0.75, 0.9))
        except Exception as exc:  # noqa: BLE001
            log.warning("SEC unavailable for backtest: %s", exc)
            filings_provider = None
    t1 = time.monotonic()
    res = backtest.run_backtest(bars, params, names=names, filings_provider=filings_provider,
                                progress=_reporter(exp_id, db_path, 0.55, 0.97))
    compute_seconds = time.monotonic() - t1
    db.insert_experiment_picks(exp_id, res["picks"], db_path)
    summary = compute_summary(res["picks"], res["baseline"], params["horizon_days"], params["hit_threshold_pct"])
    summary["stats"] = {**res["stats"], "symbols_with_bars": len(bars),
                        "download_seconds": round(dl_seconds, 1), "compute_seconds": round(compute_seconds, 1),
                        "total_seconds": round(time.monotonic() - t0, 1)}
    o, b = summary["overall"], summary["baseline"]
    msg = (f"{o['count']} picks over {res['stats']['days']} days: hit rate "
           f"{_pct(o['hit_rate'])} vs baseline {_pct(b['hit_rate'])}, avg return {_pct(o['avg_return'])} "
           f"vs {_pct(b['avg_return'])}")
    db.update_experiment(exp_id, db_path, status="completed", finished_at=utc_now_iso(), progress=1.0,
                         message=msg, summary=summary, notes=res["notes"])


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


# --------------------------------------------------------------------------- forward
def reconstruct_scan_universe(scan: dict) -> list[dict] | None:
    """Filtered universe of an old scan (before scan_universe existed) from the on-disk bar cache."""
    from . import prices

    root = config.CACHE_DIR / "bars"
    if not root.exists() or not scan.get("scan_date"):
        return None
    target = pd.Timestamp(scan["scan_date"])
    for sess in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        d = sess / f"period-{config.PRICE_PERIOD}"
        if not d.is_dir():
            continue
        bars = {t: df for t, df in prices._load_cache(d).items() if df is not None}
        if not bars or prices.as_of_date(bars) != target:
            continue
        filt = prices.filter_tradeable(bars, asof=target)
        if scan.get("filtered_size") and len(filt) != scan["filtered_size"]:
            log.warning("cache %s gives %d filtered tickers, scan says %s", sess.name, len(filt),
                        scan["filtered_size"])
            continue
        return [{"ticker": t, "close": float(df["Close"].iloc[-1])} for t, df in sorted(filt.items())]
    return None


def run_forward_experiment(exp_id: int, db_path=None, fetch=None, known_bars=None) -> None:
    exp = db.get_experiment(exp_id, db_path)
    params = dict(exp["params"])
    notes: list[str] = []
    if params.get("run_fresh_scan"):
        from . import scan

        if not scan.STATE.try_start():
            raise RuntimeError("a scan is already running")
        db.update_experiment(exp_id, db_path, progress=0.0, message="Running a fresh scan first")
        hb = threading.Event()

        def heartbeat():  # keep the experiment's heartbeat fresh and mirror scan progress
            while not hb.wait(5):
                snap = scan.STATE.snapshot()
                db.update_experiment(exp_id, db_path, progress=round(0.9 * snap["progress"], 3),
                                     message=f"Fresh scan: {snap['message']}")

        th = threading.Thread(target=heartbeat, daemon=True)
        th.start()
        try:
            params["scan_id"] = scan.run_scan(db_path=db_path, state=scan.STATE)
        finally:
            hb.set()
        db.update_experiment(exp_id, db_path, params=params)
    s = db.get_scan(params["scan_id"], db_path)
    if s is None or s["status"] != "completed":
        raise RuntimeError(f"scan {params['scan_id']} is not a completed scan")
    signal_date = s["scan_date"]
    cands = [c for c in db.list_candidates(s["id"], db_path=db_path)
             if c["verdict"] in params["verdicts"] and (c["score"] or 0) >= params["min_score"]]
    picks = [{
        "signal_date": signal_date, "ticker": c["ticker"], "name": c["name"], "score": c["score"],
        "verdict": c["verdict"], "risk_flags": [f["code"] for f in (c.get("risk_flags") or [])],
        "details": {"top_reasons": c.get("top_reasons"), "risk_flags": c.get("risk_flags"),
                    "pct_change_1d": c.get("pct_change_1d"), "rel_volume": c.get("rel_volume")},
        "ref_close": c["close"],
    } for c in cands]
    universe_rows = db.get_scan_universe(s["id"], db_path)
    if not universe_rows:
        universe_rows = reconstruct_scan_universe(s) or []
        if universe_rows:
            notes.append(f"Baseline universe ({len(universe_rows)} stocks) was reconstructed from the on-disk "
                         "price cache of that scan (scan predates baseline snapshots).")
        else:
            notes.append("Baseline unavailable: the scan's filtered universe was not stored and its price "
                         "cache is gone. Only the picks are evaluated.")
    baseline = [{"signal_date": signal_date, "ticker": r["ticker"], "ref_close": r["close"], "is_baseline": 1}
                for r in universe_rows]
    db.insert_experiment_picks(exp_id, picks + baseline, db_path)
    due = tracking.add_trading_days(dt.date.fromisoformat(signal_date), params["horizon_days"])
    notes += [
        f"Predictions frozen from scan #{s['id']} (as of {signal_date}): {len(picks)} picks with verdict in "
        f"{', '.join(params['verdicts'])} and score >= {params['min_score']:g}. Baseline = all "
        f"{len(baseline)} stocks that passed the price/liquidity filter in that scan (picks included).",
        ("Entry = next trading day's open; " if params["entry"] == "next_open" else "Entry = signal-day close; ")
        + f"window = the next {params['horizon_days']} bars; hit = max gain >= {params['hit_threshold_pct']:g}%. "
        "While waiting, returns are mark-to-market on the bars so far and hit is only set once the full "
        "window exists.",
        "One scan is a small sample: a single forward test says little on its own. Run one after many "
        "scans and compare them, or use a backtest for a larger sample.",
        "Fills at the open are optimistic for illiquid penny stocks (spread, slippage); no costs modelled.",
        f"Due date {due.isoformat()} assumes no exchange holiday in the window (holidays are not modelled; "
        "evaluation simply waits for the bars).",
    ]
    db.update_experiment(exp_id, db_path, status="waiting", scan_id=s["id"], signal_date=signal_date,
                         due_date=due.isoformat(), notes=notes, progress=0.0,
                         message=f"Waiting for {params['horizon_days']} bars after {signal_date}")
    evaluate_forward(exp_id, db_path, fetch=fetch, known_bars=known_bars)


def _default_fetch(tickers: list[str], start: str) -> dict:
    from .prices import download_bars

    return download_bars(tickers, start=start, use_cache=True, chunk_size=100)


def evaluate_forward(exp_id: int, db_path=None, fetch=None, known_bars: dict | None = None) -> dict:
    """(Re-)evaluate one forward experiment from fresh bars. Never raises for data problems."""
    with EVAL_LOCK:
        exp = db.get_experiment(exp_id, db_path)
        if exp is None or exp["kind"] != "forward" or exp["status"] not in ("waiting", "completed"):
            return {"updated": 0}
        params, signal_date = exp["params"], exp["signal_date"]
        if not signal_date:
            return {"updated": 0}
        horizon, entry, thr = params["horizon_days"], params["entry"], params["hit_threshold_pct"]
        picks = db.get_experiment_picks(exp_id, False, db_path)
        base = db.get_experiment_picks(exp_id, True, db_path)
        rows = picks + base
        tickers = sorted({r["ticker"] for r in rows})
        bars: dict = {}
        need = []
        for t in tickers:
            df = (known_bars or {}).get(t)
            if df is not None and len(df) and df.index[0] <= pd.Timestamp(signal_date):
                bars[t] = df
            else:
                need.append(t)
        settled = tracking.last_settled_session()
        fetch_error = None
        if need and settled.isoformat() > signal_date:
            start_d = (dt.date.fromisoformat(signal_date) - dt.timedelta(days=10)).isoformat()
            try:
                bars.update((fetch or _default_fetch)(need, start_d))
            except Exception as exc:  # noqa: BLE001
                fetch_error = str(exc)
                log.warning("forward evaluation fetch failed: %s", exc)
        updates = []
        for r in rows:
            df = bars.get(r["ticker"])
            if df is None and r["status"] != "pending":
                continue  # keep the last known values if this fetch missed the ticker
            o = tracking.window_outcome(df, signal_date, horizon, entry, thr, ref_close=r["ref_close"])
            r.update(o)
            updates.append((r["id"], o))
        db.update_pick_outcomes(updates, db_path)
        summary = compute_summary(picks, base, horizon, thr)
        due = dt.date.fromisoformat(exp["due_date"])
        all_final = all(p["status"] == "final" for p in picks)
        grace_over = settled >= tracking.add_trading_days(due, COMPLETE_GRACE_DAYS)
        done = settled >= due and (all_final or grace_over)
        counted = picks or base
        bars_so_far = max((r["bars_available"] or 0 for r in counted), default=0)
        o, b = summary["overall"], summary["baseline"]
        if done:
            msg = (f"Completed: {o['evaluated']}/{o['count']} picks evaluated, hit rate {_pct(o['hit_rate'])} "
                   f"vs baseline {_pct(b['hit_rate'])}, avg return {_pct(o['avg_return'])} vs "
                   f"{_pct(b['avg_return'])}")
            fields = {"status": "completed", "progress": 1.0,
                      "finished_at": exp.get("finished_at") or utc_now_iso()}
        else:
            msg = (f"Waiting: {bars_so_far}/{horizon} bars so far, due {exp['due_date']}; mark-to-market avg "
                   f"return {_pct(o['avg_return'])} vs baseline {_pct(b['avg_return'])}")
            fields = {"status": "waiting", "progress": round(bars_so_far / horizon, 4)}
        if fetch_error:
            msg += f" | last fetch failed: {fetch_error}"
        summary["last_evaluated_at"] = utc_now_iso()
        summary["last_settled_session"] = settled.isoformat()
        db.update_experiment(exp_id, db_path, summary=summary, message=msg, **fields)
        return {"updated": len(updates), "status": fields["status"]}


def evaluate_waiting(db_path=None, fetch=None, known_bars: dict | None = None) -> dict:
    """Evaluate every waiting forward experiment (end of scan, startup, endpoint/CLI)."""
    out = {}
    for exp in db.list_experiments(db_path, status="waiting", kind="forward"):
        try:
            out[exp["id"]] = evaluate_forward(exp["id"], db_path, fetch=fetch, known_bars=known_bars)
        except Exception as exc:  # noqa: BLE001
            log.warning("evaluation of experiment %s failed: %s", exp["id"], exc)
            out[exp["id"]] = {"error": str(exc)}
    return out


def evaluate_waiting_background(db_path=None) -> threading.Thread | None:
    if not db.list_experiments(db_path, status="waiting", kind="forward"):
        return None
    th = threading.Thread(target=evaluate_waiting, args=(db_path,), name="tradealert-exp-eval", daemon=True)
    th.start()
    return th


# --------------------------------------------------------------------------- API shaping
def _headline(summary: dict | None) -> dict | None:
    if not summary or "overall" not in summary:
        return None
    o, b = summary["overall"], summary["baseline"]
    return {"picks": o["count"], "evaluated": o["evaluated"], "hit_rate": o["hit_rate"],
            "baseline_hit_rate": b["hit_rate"], "avg_return": o["avg_return"],
            "baseline_avg_return": b["avg_return"]}


def public_item(exp: dict) -> dict:
    progress = exp.get("progress")
    if exp["status"] == "completed":
        progress = 1.0
    elif exp["status"] == "failed":
        progress = None
    return {
        "id": exp["id"], "kind": exp["kind"], "name": exp["name"], "status": exp["status"],
        "created_at": exp["created_at"], "finished_at": exp["finished_at"], "due_date": exp["due_date"],
        "params": exp["params"] or {}, "progress": progress, "message": exp.get("message"),
        "headline": _headline(exp.get("summary")) if exp["status"] != "running" else None,
    }


def public_pick(p: dict) -> dict:
    return {
        "signal_date": p["signal_date"], "ticker": p["ticker"], "name": p["name"], "score": p["score"],
        "verdict": p["verdict"], "entry_date": p["entry_date"], "entry_price": p["entry_price"],
        "exit_date": p["exit_date"], "exit_price": p["exit_price"], "return": p["ret"],
        "max_gain": p["max_gain"], "max_drawdown": p["max_drawdown"],
        "hit": None if p["hit"] is None else bool(p["hit"]), "status": p["status"],
        "risk_flags": p["risk_flags"] or [],
    }


EMPTY_SUMMARY = {"overall": group_stats(pd.DataFrame()), "baseline": group_stats(pd.DataFrame()),
                 "by_verdict": [], "by_month": [], "equity_curve": []}


def public_detail(exp: dict, db_path=None) -> dict:
    d = public_item(exp)
    picks = db.get_experiment_picks(exp["id"], False, db_path, limit=PICKS_CAP)
    d.update({
        "notes": exp.get("notes") or [], "error": exp.get("error"),
        "summary": exp.get("summary") or EMPTY_SUMMARY,
        "picks": [public_pick(p) for p in picks],
        "picks_total": db.count_experiment_picks(exp["id"], False, db_path),
        "scan_id": exp.get("scan_id"), "signal_date": exp.get("signal_date"),
        "started_at": exp.get("started_at"),
    })
    return d


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import logging

    ap = argparse.ArgumentParser(prog="run.py experiment", description="TradeAlert experiments")
    sub = ap.add_subparsers(dest="cmd", required=True)
    bt = sub.add_parser("backtest", help="replay the scanner over a past date range")
    bt.add_argument("--start", dest="start_date")
    bt.add_argument("--end", dest="end_date")
    bt.add_argument("--horizon", dest="horizon_days", type=int)
    bt.add_argument("--hit", dest="hit_threshold_pct", type=float)
    bt.add_argument("--top-n", dest="top_n", type=int)
    bt.add_argument("--min-score", dest="min_score", type=float)
    bt.add_argument("--entry", choices=tracking.ENTRY_MODES)
    bt.add_argument("--no-filings", dest="include_filings", action="store_false", default=None)
    bt.add_argument("--name")
    fw = sub.add_parser("forward", help="freeze the latest scan's picks and evaluate them later")
    fw.add_argument("--scan-id", type=int)
    fw.add_argument("--fresh-scan", dest="run_fresh_scan", action="store_true", default=None)
    fw.add_argument("--verdicts", help="comma-separated, default STRONG,WATCH")
    fw.add_argument("--min-score", dest="min_score", type=float)
    fw.add_argument("--horizon", dest="horizon_days", type=int)
    fw.add_argument("--hit", dest="hit_threshold_pct", type=float)
    fw.add_argument("--entry", choices=tracking.ENTRY_MODES)
    fw.add_argument("--name")
    ev = sub.add_parser("evaluate", help="evaluate waiting forward experiments now")
    ev.add_argument("--id", type=int)
    sub.add_parser("list", help="list experiments")
    sh = sub.add_parser("show", help="print one experiment as JSON")
    sh.add_argument("id", type=int)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    db.init_db()
    db.mark_stale_running_experiments()

    if args.cmd in ("backtest", "forward"):
        keys = (("start_date", "end_date", "horizon_days", "hit_threshold_pct", "top_n", "min_score", "entry",
                 "include_filings") if args.cmd == "backtest" else
                ("scan_id", "run_fresh_scan", "min_score", "horizon_days", "hit_threshold_pct", "entry"))
        params = {k: getattr(args, k) for k in keys if getattr(args, k) is not None}
        if args.cmd == "forward" and args.verdicts:
            params["verdicts"] = [v.strip() for v in args.verdicts.split(",") if v.strip()]
        try:
            exp_id = start(args.cmd, params, name=args.name, background=False)
        except (ParamError, Conflict) as exc:
            print(f"Error: {exc}")
            return 2
        exp = db.get_experiment(exp_id)
        print(f"\nExperiment #{exp_id} ({exp['kind']}) {exp['status']}: {exp['message']}")
        if exp.get("error"):
            return 1
        _print_summary(exp)
        return 0
    if args.cmd == "evaluate":
        res = ({args.id: evaluate_forward(args.id)} if args.id else evaluate_waiting())
        for k, v in res.items():
            e = db.get_experiment(k)
            print(f"#{k}: {e['status']} - {e['message']}")
        if not res:
            print("No waiting forward experiments.")
        return 0
    if args.cmd == "list":
        for e in db.list_experiments():
            print(f"#{e['id']:<4} {e['kind']:<8} {e['status']:<9} {e['created_at']}  {e['name']}\n      {e['message']}")
        return 0
    e = db.get_experiment(args.id)
    if e is None:
        print("not found")
        return 1
    from .utils import clean

    print(json.dumps(clean(public_detail(e)), indent=2)[:20000])
    return 0


def _print_summary(exp: dict) -> None:
    s = exp.get("summary") or {}
    if not s:
        return
    rows = [("ALL picks", s["overall"]), ("BASELINE", s["baseline"])] + [
        (v["verdict"], v) for v in s.get("by_verdict", []) if v["count"]]
    print(f"{'GROUP':<10} {'N':>6} {'EVAL':>6} {'HIT%':>6} {'AVG RET':>8} {'MED RET':>8} {'WIN%':>6} "
          f"{'MAXGAIN':>8} {'MAXDD':>7} {'BASE HIT%':>9} {'BASE RET':>8}")
    for name, g in rows:
        def p(x):
            return "n/a" if x is None else f"{x * 100:.1f}"
        print(f"{name:<10} {g['count']:>6} {g['evaluated']:>6} {p(g['hit_rate']):>6} {p(g['avg_return']):>8} "
              f"{p(g['median_return']):>8} {p(g['win_rate']):>6} {p(g['avg_max_gain']):>8} "
              f"{p(g['avg_max_drawdown']):>7} {p(g.get('baseline_hit_rate')):>9} "
              f"{p(g.get('baseline_avg_return')):>8}")

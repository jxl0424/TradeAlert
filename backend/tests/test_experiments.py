"""Experiments: backtest replay (no look-ahead, outcome maths, baseline), forward lifecycle,
DB migration and API shape. No network: bars and filings are synthetic."""
import datetime as dt
import math
import sqlite3
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradealert import api, backtest, config, db, experiments, filings, scan, scoring, tracking
from tradealert.signals import compute_signals

from .conftest import make_bars


# --------------------------------------------------------------------------- helpers
def random_bars(seed, n=160, start="2026-01-02", price=2.0, vol=1_000_000.0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.05, n)
    rets[rng.random(n) < 0.03] += rng.uniform(0.2, 0.6)  # occasional spikes
    closes = price * np.exp(np.cumsum(rets))
    vols = vol * np.exp(rng.normal(0, 0.6, n))
    opens = np.r_[closes[0], closes[:-1]] * np.exp(rng.normal(0, 0.03, n))
    highs = np.maximum(opens, closes) * (1 + rng.uniform(0, 0.08, n))
    lows = np.minimum(opens, closes) * (1 - rng.uniform(0, 0.08, n))
    return make_bars(closes, vols, start=start, opens=opens, highs=highs, lows=lows)


def universe(n=25, **kw):
    return {f"T{i:02d}": random_bars(i, **kw) for i in range(n)}


BT_PARAMS = {"horizon_days": 5, "hit_threshold_pct": 20.0, "top_n": 3, "min_score": 0.0,
             "entry": "next_open", "include_filings": False}


def bt_params(bars, i0=80, i1=140, **kw):
    idx = next(iter(bars.values())).index
    return {**BT_PARAMS, "start_date": idx[i0].strftime("%Y-%m-%d"), "end_date": idx[i1].strftime("%Y-%m-%d"),
            **kw}


# --------------------------------------------------------------------------- signals parity
SCORE_KEYS = ["rel_volume", "vol_trend_3d", "pct_change_1d", "pct_change_5d", "breakout_20d", "gap_pct",
              "close_position", "range_expansion", "squeeze_ratio", "avg_dollar_volume_20d", "close", "open"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_vectorised_signals_match_live_compute_signals(seed):
    df = random_bars(seed, n=150)
    vec = backtest.ticker_signals(df)
    L = backtest.LOOKBACK_BARS
    for i in (25, 40, 64, 65, 66, 100, 149):
        window = df.iloc[max(0, i - L + 1): i + 1]
        live = compute_signals(window)
        row = backtest._signal_dict(vec.iloc[i].to_dict())
        for k in SCORE_KEYS:
            a, b = live[k], row[k]
            if a is None or b is None:
                assert a is None and b is None, (i, k, a, b)
            else:
                assert b == pytest.approx(a, rel=1e-9, abs=1e-12), (i, k)
        assert scoring.technical_score(row) == pytest.approx(scoring.technical_score(live), abs=1e-9)


# --------------------------------------------------------------------------- outcome windows
def test_window_outcome_next_open_and_close_entries():
    closes = [1.0] * 21 + [1.1, 1.2, 1.3, 1.4, 1.5, 1.6]
    opens = [1.0] * 21 + [1.05, 1.1, 1.2, 1.3, 1.4, 1.5]
    df = make_bars(closes, opens=opens, spread=0.0)
    d = df.index[20].strftime("%Y-%m-%d")
    o = tracking.window_outcome(df, d, 5, "next_open", 20)
    assert o["entry_date"] == df.index[21].strftime("%Y-%m-%d") and o["entry_price"] == pytest.approx(1.05)
    assert o["exit_date"] == df.index[25].strftime("%Y-%m-%d") and o["exit_price"] == pytest.approx(1.5)
    assert o["ret"] == pytest.approx(1.5 / 1.05 - 1)
    assert o["max_gain"] == pytest.approx(1.5 / 1.05 - 1)       # highest high in bars 21..25 = 1.5
    assert o["max_drawdown"] == pytest.approx(1.05 / 1.05 - 1)  # lowest low = bar 21 low = open 1.05
    assert o["hit"] is True and o["status"] == "final" and o["bars_available"] == 5
    c = tracking.window_outcome(df, d, 5, "close", 60)
    assert c["entry_date"] == d and c["entry_price"] == pytest.approx(1.0)
    assert c["ret"] == pytest.approx(0.5) and c["hit"] is False  # +50% < 60% threshold


def test_window_outcome_partial_and_pending():
    df = make_bars([1.0] * 21 + [1.3, 1.1])
    d = df.index[20].strftime("%Y-%m-%d")
    o = tracking.window_outcome(df, d, 5, "next_open", 20)
    assert o["status"] == "partial" and o["hit"] is None and o["bars_available"] == 2
    assert o["exit_price"] == pytest.approx(1.1)
    assert o["max_gain"] is not None and o["max_gain"] > 0.2  # would be a hit, but window not complete
    last = df.index[-1].strftime("%Y-%m-%d")
    p = tracking.window_outcome(df, last, 5, "next_open", 20)
    assert p["status"] == "pending" and p["entry_price"] is None and p["ret"] is None
    q = tracking.window_outcome(df, last, 5, "close", 20)
    assert q["status"] == "pending" and q["entry_price"] == pytest.approx(1.1)


@pytest.mark.parametrize("entry", ["next_open", "close"])
def test_array_outcomes_match_scalar(entry):
    df = random_bars(7, n=60)
    arr = tracking.window_outcomes_array(df["Open"].values, df["High"].values, df["Low"].values,
                                         df["Close"].values, 5, entry)
    for i in range(len(df)):
        o = tracking.window_outcome(df, df.index[i].strftime("%Y-%m-%d"), 5, entry, 20)
        for k_arr, k in (("ret", "ret"), ("max_gain", "max_gain"), ("max_drawdown", "max_drawdown")):
            if o[k] is None:
                assert not math.isfinite(arr[k_arr][i]) or o["status"] == "pending", (i, k)
            else:
                assert arr[k_arr][i] == pytest.approx(o[k]), (i, k)
        assert int(arr["n_avail"][i]) == o["bars_available"]


# --------------------------------------------------------------------------- backtest replay
def test_no_lookahead_spike_on_next_day_does_not_change_selection():
    bars = universe(20)
    p = bt_params(bars)
    res_a = backtest.run_backtest(bars, p)
    day = next(iter(bars.values())).index[100]
    d = day.strftime("%Y-%m-%d")
    # A huge spike on D+1 (high, close, volume; the D+1 open = entry is unchanged) for a ticker
    # that was NOT picked on D must not change D's (or any earlier day's) scores or picks.
    picked_a = {x["ticker"] for x in res_a["picks"] if x["signal_date"] == d}
    victim = next(t for t in sorted(bars) if t not in picked_a)
    tampered = {t: df.copy() for t, df in bars.items()}
    nxt = tampered[victim].index[101]
    tampered[victim].loc[nxt, ["High", "Close"]] *= 5
    tampered[victim].loc[nxt, "Volume"] *= 50
    res_b = backtest.run_backtest(tampered, p)

    def day_picks(res, upto):
        return [(x["signal_date"], x["ticker"], x["score"], x["verdict"]) for x in res["picks"]
                if x["signal_date"] <= upto]

    assert day_picks(res_a, d) == day_picks(res_b, d)
    # ... while the victim's own outcome for D does see the future (sanity of the tamper)
    ba = res_a["baseline"].set_index(["signal_date", "ticker"])
    bb = res_b["baseline"].set_index(["signal_date", "ticker"])
    if (d, victim) in ba.index:
        assert bb.loc[(d, victim), "max_gain"] > ba.loc[(d, victim), "max_gain"] + 1


def test_backtest_selection_matches_live_scoring_and_outcomes():
    bars = universe(15)
    p = bt_params(bars, top_n=2)
    res = backtest.run_backtest(bars, p)
    assert res["picks"], "expected some picks"
    by_day = {}
    for x in res["picks"]:
        by_day.setdefault(x["signal_date"], []).append(x)
    assert all(len(v) <= 2 for v in by_day.values())
    scale = 100.0 / 65.0
    for x in res["picks"][:15]:
        df = bars[x["ticker"]]
        i = df.index.get_loc(pd.Timestamp(x["signal_date"]))
        live = compute_signals(df.iloc[max(0, i - 64): i + 1])
        flags = scoring.technical_risk_flags(live)
        want = round(max(0, min(100, scoring.technical_score(live) * scale
                                + scoring.penalty_component(flags)["points"])), 1)
        assert x["score"] == pytest.approx(want, abs=0.11)
        assert x["verdict"] == scoring.verdict_for(x["score"], flags)
        o = tracking.window_outcome(df, x["signal_date"], 5, "next_open", 20)
        assert x["entry_price"] == pytest.approx(o["entry_price"]) and x["ret"] == pytest.approx(o["ret"])
        assert x["entry_date"] == o["entry_date"] and x["exit_date"] == o["exit_date"]
        assert x["hit"] == o["hit"] and x["status"] == o["status"]
    # baseline is every filtered stock-day, picks included
    base = res["baseline"]
    assert len(base) >= len(res["picks"])
    keys = set(zip(base["signal_date"], base["ticker"]))
    assert all((x["signal_date"], x["ticker"]) in keys for x in res["picks"])


def test_penny_filter_is_applied_as_of_each_day():
    # 'RISE' trades at $8 until bar 99, then $3: only eligible from bar 100 on (20d ADV needs no wait here).
    closes = [8.0] * 100 + [3.0] * 60
    bars = {"RISE": make_bars(closes, start="2026-01-02"), **universe(5)}
    res = backtest.run_backtest(bars, bt_params(bars, 80, 140))
    days = set(res["baseline"].loc[res["baseline"]["ticker"] == "RISE", "signal_date"])
    idx = bars["RISE"].index
    assert idx[99].strftime("%Y-%m-%d") not in days
    assert idx[100].strftime("%Y-%m-%d") in days


def test_filings_replay_respects_dates_and_acceptance_time():
    bars = universe(6)
    idx = next(iter(bars.values())).index
    D = idx[100].date()
    hist = {"filings": [
        # 424B5 accepted at 18:00 ET on D: public only after the D scan -> flags from D+1
        {"form": "424B5", "filed_at": D.isoformat(), "accepted_et": f"{D.isoformat()}T18:00:00-05:00",
         **{k: v for k, v in filings.classify_filing("424B5").items() if k in ("category", "flags", "description",
                                                                             "catalyst_items")}},
    ], "oldest": D.isoformat(), "has_more_files": False}
    assert not filings.visible_on(hist["filings"][0], D)
    assert filings.visible_on(hist["filings"][0], D + dt.timedelta(days=1))
    s_d = filings.filings_as_of(hist["filings"], D)
    s_d1 = filings.filings_as_of(hist["filings"], idx[101].date())
    assert s_d["risk_flags"] == [] and s_d1["risk_flags"][0]["severity"] == "high"

    def provider(tickers, through):
        return {t: hist for t in tickers}

    res = backtest.run_backtest(bars, bt_params(bars, 95, 110, top_n=6, include_filings=True),
                                filings_provider=provider)
    before = [x for x in res["picks"] if x["signal_date"] <= D.isoformat()]
    fresh_end = (D + dt.timedelta(days=10)).isoformat()  # flags older than 10 days are downgraded
    after = [x for x in res["picks"] if D.isoformat() < x["signal_date"] <= fresh_end]
    later = [x for x in res["picks"] if x["signal_date"] > fresh_end]
    assert before and after and later
    assert all(x["verdict"] != "AVOID" and "DILUTION_PROSPECTUS" in x["risk_flags"] for x in later)
    assert all(x["verdict"] != "AVOID" or "DILUTION_PROSPECTUS" not in x["risk_flags"] for x in before)
    assert all(x["verdict"] == "AVOID" and "DILUTION_PROSPECTUS" in x["risk_flags"] for x in after)
    assert any("SEC filings replayed" in n for n in res["notes"])


# --------------------------------------------------------------------------- summary maths
def test_summary_baseline_maths():
    picks = [
        {"signal_date": "2026-03-02", "verdict": "STRONG", "ret": 0.30, "max_gain": 0.40, "max_drawdown": -0.05,
         "hit": True, "status": "final"},
        {"signal_date": "2026-03-02", "verdict": "WATCH", "ret": -0.10, "max_gain": 0.05, "max_drawdown": -0.2,
         "hit": False, "status": "final"},
        {"signal_date": "2026-04-01", "verdict": "STRONG", "ret": 0.10, "max_gain": 0.25, "max_drawdown": -0.1,
         "hit": True, "status": "final"},
        {"signal_date": "2026-04-01", "verdict": "WEAK", "ret": None, "max_gain": None, "max_drawdown": None,
         "hit": None, "status": "pending"},
    ]
    base = pd.DataFrame({
        "signal_date": ["2026-03-02"] * 4 + ["2026-04-01"] * 2,
        "ret": [0.30, -0.10, 0.0, -0.2, 0.10, 0.0],
        "max_gain": [0.40, 0.05, 0.0, 0.0, 0.25, 0.1],
        "max_drawdown": [-0.05, -0.2, -0.1, -0.3, -0.1, -0.1],
        "hit": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        "status": ["final"] * 6,
    })
    s = experiments.compute_summary(picks, base, horizon=5, hit_threshold_pct=20)
    o, b = s["overall"], s["baseline"]
    assert o["count"] == 4 and o["evaluated"] == 3
    assert o["hit_rate"] == pytest.approx(2 / 3)
    assert o["avg_return"] == pytest.approx((0.3 - 0.1 + 0.1) / 3)
    assert o["median_return"] == pytest.approx(0.1)
    assert o["win_rate"] == pytest.approx(2 / 3)
    assert o["avg_max_drawdown"] == pytest.approx((-0.05 - 0.2 - 0.1) / 3)
    assert b["count"] == 6 and b["hit_rate"] == pytest.approx(2 / 6)
    assert b["avg_return"] == pytest.approx(0.1 / 6)
    strong = next(v for v in s["by_verdict"] if v["verdict"] == "STRONG")
    assert strong["count"] == 2 and strong["hit_rate"] == 1.0
    assert strong["baseline_hit_rate"] == pytest.approx(2 / 6)  # baseline on the same days
    watch = next(v for v in s["by_verdict"] if v["verdict"] == "WATCH")
    assert watch["baseline_avg_return"] == pytest.approx(0.0)    # only 2026-03-02's 4 stocks
    assert [m["month"] for m in s["by_month"]] == ["2026-03", "2026-04"]
    assert s["by_month"][1]["baseline_hit_rate"] == pytest.approx(0.5)
    eq = s["equity_curve"]
    assert [e["date"] for e in eq] == ["2026-03-02", "2026-04-01"]
    assert eq[0]["picks_avg_return"] == pytest.approx(0.1)
    assert eq[1]["cumulative_picks"] == pytest.approx((1 + 0.1 / 5) * (1 + 0.1 / 5) - 1)
    assert s["edge"]["hit_rate"] == pytest.approx(2 / 3 - 1 / 3)


def test_summary_partial_basis_when_nothing_final():
    picks = [{"signal_date": "2026-11-12", "verdict": "STRONG", "ret": 0.05, "max_gain": 0.3,
              "max_drawdown": -0.01, "hit": None, "status": "partial"}]
    s = experiments.compute_summary(picks, [], 5, 20)
    assert s["overall"]["basis"] == "partial" and s["overall"]["hit_rate"] is None
    assert s["overall"]["avg_return"] == pytest.approx(0.05) and s["overall"]["evaluated"] == 0
    assert s["baseline"]["count"] == 0 and s["baseline"]["hit_rate"] is None


# --------------------------------------------------------------------------- persistence
def test_backtest_experiment_persists(tmp_db):
    bars = universe(10)
    p = experiments.validate_params  # noqa: F841 - validated separately below
    params = bt_params(bars, top_n=2)
    exp_id = db.create_experiment("backtest", "t", params, tmp_db)
    experiments.run_backtest_experiment(exp_id, tmp_db, bars=bars, names={t: f"{t} Inc" for t in bars})
    e = db.get_experiment(exp_id, tmp_db)
    assert e["status"] == "completed" and e["progress"] == 1.0
    assert e["summary"]["overall"]["count"] == db.count_experiment_picks(exp_id, False, tmp_db)
    assert db.count_experiment_picks(exp_id, True, tmp_db) == 0  # backtest baseline is aggregated
    assert any("Survivorship" in n for n in e["notes"])
    d = experiments.public_detail(e, tmp_db)
    assert d["picks"][0]["name"].endswith("Inc")
    assert d["picks"] == sorted(d["picks"], key=lambda x: -x["score"])


# --------------------------------------------------------------------------- forward lifecycle
def _scan_with_universe(path, scan_date, cands, universe_rows):
    sid = db.create_scan({}, path)
    db.update_scan(sid, path, status="completed", scan_date=scan_date, finished_at="2026-11-12T22:00:00+00:00",
                   candidates_count=len(cands), filtered_size=len(universe_rows))
    db.insert_candidates(sid, scan_date, cands, path)
    db.insert_scan_universe(sid, universe_rows, path)
    return sid


def test_forward_lifecycle_waiting_partial_completed(tmp_db, monkeypatch):
    up = make_bars([1.0] * 25 + [1.1, 1.2, 1.3, 1.4, 1.5], start="2026-10-07", spread=0.0)
    flat = make_bars([2.0] * 30, start="2026-10-07", spread=0.01)
    signal = up.index[24]
    sd = signal.strftime("%Y-%m-%d")
    sid = _scan_with_universe(tmp_db, sd, [
        {"ticker": "UPP", "name": "Up Inc", "close": 1.0, "score": 80, "verdict": "STRONG",
         "risk_flags": [{"code": "SUB_DOLLAR", "severity": "low", "text": "x"}]},
        {"ticker": "FLT", "name": "Flat", "close": 2.0, "score": 50, "verdict": "WATCH", "risk_flags": []},
        {"ticker": "BAD", "name": "Bad", "close": 1.0, "score": 70, "verdict": "AVOID", "risk_flags": []},
    ], [("UPP", 1.0), ("FLT", 2.0), ("BAD", 1.0), ("OTH", 2.0)])
    full = {"UPP": up, "FLT": flat, "BAD": flat * 0.5, "OTH": flat}
    state = {"upto": signal, "settled": signal.date()}
    monkeypatch.setattr(tracking, "last_settled_session", lambda now=None: state["settled"])

    def fake_fetch(tickers, start):
        return {t: full[t][full[t].index <= state["upto"]] for t in tickers if t in full}

    # 1) snapshot on the signal day itself: waiting, everything pending
    exp_id = db.create_experiment("forward", "fw", experiments.validate_params("forward", {}, tmp_db), tmp_db)
    experiments.run_forward_experiment(exp_id, tmp_db, fetch=fake_fetch)
    e = db.get_experiment(exp_id, tmp_db)
    assert e["status"] == "waiting" and e["scan_id"] == sid and e["signal_date"] == sd
    assert e["due_date"] == tracking.add_trading_days(signal.date(), 5).isoformat()
    picks = db.get_experiment_picks(exp_id, False, tmp_db)
    assert [p["ticker"] for p in picks] == ["UPP", "FLT"]  # AVOID excluded by default verdicts
    assert db.count_experiment_picks(exp_id, True, tmp_db) == 4
    assert all(p["status"] == "pending" for p in picks)

    # 2) two bars later: partial, returns shown, no hit yet
    state["upto"], state["settled"] = up.index[26], up.index[26].date()
    experiments.evaluate_forward(exp_id, tmp_db, fetch=fake_fetch)
    e = db.get_experiment(exp_id, tmp_db)
    upp = next(p for p in db.get_experiment_picks(exp_id, False, tmp_db) if p["ticker"] == "UPP")
    assert e["status"] == "waiting" and upp["status"] == "partial" and upp["hit"] is None
    assert upp["entry_price"] == pytest.approx(1.0) and upp["ret"] == pytest.approx(0.2)  # next open = 1.0
    assert e["summary"]["overall"]["basis"] == "partial" and e["progress"] == pytest.approx(0.4)

    # 3) full window: completed, hit set; via the scan hook with known bars (no fetch at all)
    state["upto"], state["settled"] = up.index[29], up.index[29].date()

    def no_fetch(tickers, start):
        raise AssertionError(f"should use known bars, fetched {tickers}")

    known = {t: df for t, df in full.items()}
    experiments.evaluate_waiting(tmp_db, fetch=no_fetch, known_bars=known)
    e = db.get_experiment(exp_id, tmp_db)
    assert e["status"] == "completed" and e["finished_at"]
    upp = next(p for p in db.get_experiment_picks(exp_id, False, tmp_db) if p["ticker"] == "UPP")
    assert upp["status"] == "final" and upp["hit"] == 1 and upp["ret"] == pytest.approx(0.5)
    o, b = e["summary"]["overall"], e["summary"]["baseline"]
    assert o["evaluated"] == 2 and o["hit_rate"] == pytest.approx(0.5)
    assert b["count"] == 4 and b["evaluated"] == 4 and b["hit_rate"] == pytest.approx(0.25)


def test_forward_uses_entry_close_and_min_score(tmp_db, monkeypatch):
    bars = make_bars([1.0] * 25 + [1.2] * 5, start="2026-10-07", spread=0.0)
    sd = bars.index[24].strftime("%Y-%m-%d")
    _scan_with_universe(tmp_db, sd, [
        {"ticker": "A", "close": 1.0, "score": 80, "verdict": "STRONG", "risk_flags": []},
        {"ticker": "B", "close": 1.0, "score": 46, "verdict": "WATCH", "risk_flags": []},
    ], [("A", 1.0), ("B", 1.0)])
    monkeypatch.setattr(tracking, "last_settled_session", lambda now=None: bars.index[-1].date())
    params = experiments.validate_params("forward", {"entry": "close", "min_score": 60, "verdicts": ["strong", "WATCH"]},
                                         tmp_db)
    exp_id = db.create_experiment("forward", "x", params, tmp_db)
    experiments.run_forward_experiment(exp_id, tmp_db, fetch=lambda t, s: {x: bars for x in t})
    picks = db.get_experiment_picks(exp_id, False, tmp_db)
    assert [p["ticker"] for p in picks] == ["A"]
    assert picks[0]["entry_date"] == sd and picks[0]["ret"] == pytest.approx(0.2)
    assert db.get_experiment(exp_id, tmp_db)["status"] == "completed"


def test_forward_baseline_reconstructed_from_cache(tmp_db, monkeypatch):
    import pickle

    bars = {"AAA": make_bars([1.0] * 30, start="2026-10-01"), "BBB": make_bars([9.0] * 30, start="2026-10-01"),
            "CCC": make_bars([2.0] * 30, start="2026-10-01")}
    sd = bars["AAA"].index[-1].strftime("%Y-%m-%d")
    d = config.CACHE_DIR / "bars" / "2026-11-13-intraday" / f"period-{config.PRICE_PERIOD}"
    d.mkdir(parents=True)
    with (d / "chunk_x.pkl").open("wb") as fh:
        pickle.dump(bars, fh)
    sid = db.create_scan({}, tmp_db)
    db.update_scan(sid, tmp_db, status="completed", scan_date=sd, filtered_size=2)
    rows = experiments.reconstruct_scan_universe(db.get_scan(sid, tmp_db))
    assert [r["ticker"] for r in rows] == ["AAA", "CCC"]  # BBB is above $5


# --------------------------------------------------------------------------- migration
OLD_SCHEMA = """
CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, scan_date TEXT, started_at TEXT NOT NULL,
  finished_at TEXT, status TEXT NOT NULL, universe_size INTEGER, filtered_size INTEGER,
  candidates_count INTEGER, message TEXT, params TEXT);
CREATE TABLE candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL, scan_date TEXT,
  ticker TEXT NOT NULL, name TEXT, score REAL, verdict TEXT, UNIQUE(scan_id, ticker));
CREATE TABLE experiments (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL);
"""


def test_migration_of_old_db_keeps_data(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.execute("INSERT INTO scans (scan_date, started_at, status) VALUES ('2026-11-12', 'x', 'completed')")
    con.execute("INSERT INTO candidates (scan_id, ticker, score, verdict) VALUES (1, 'ABC', 70, 'STRONG')")
    con.execute("INSERT INTO experiments (kind, status, created_at) VALUES ('backtest', 'completed', 'x')")
    con.commit()
    con.close()
    db.init_db(path)
    db.init_db(path)  # idempotent
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"experiments", "experiment_picks", "scan_universe", "outcomes"} <= tables
    cols = {r[1] for r in con.execute("PRAGMA table_info(experiments)")}
    assert {"params", "summary", "notes", "due_date", "error", "progress", "message"} <= cols
    assert con.execute("SELECT ticker, score FROM candidates").fetchall() == [("ABC", 70.0)]
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    con.close()
    e = db.get_experiment(1, path)
    assert e["kind"] == "backtest" and e["params"] is None
    assert experiments.public_item(e)["headline"] is None


# --------------------------------------------------------------------------- params
def test_param_validation():
    d = experiments.defaults()
    assert d["backtest"]["params"]["horizon_days"] == 5 and d["backtest"]["params"]["top_n"] == 10
    b = experiments.validate_params("backtest", {})
    start, end = dt.date.fromisoformat(b["start_date"]), dt.date.fromisoformat(b["end_date"])
    assert 175 <= (end - start).days <= 186
    assert end <= tracking.add_trading_days(end, 6) <= tracking.last_settled_session() + dt.timedelta(days=3)
    for bad in ({"horizon_days": 0}, {"horizon_days": "5"}, {"top_n": 2.5}, {"entry": "vwap"},
                {"start_date": "2026-13-01"}, {"start_date": "2026-06-01", "end_date": "2026-05-01"},
                {"start_date": "2020-01-01"}, {"include_filings": "yes"}, {"bogus": 1}, {"min_score": float("nan")}):
        with pytest.raises(experiments.ParamError):
            experiments.validate_params("backtest", bad)
    with pytest.raises(experiments.ParamError):
        experiments.validate_params("nope", {})


# --------------------------------------------------------------------------- API
@pytest.fixture
def client(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FRONTEND_DIR", tmp_path / "no-frontend")
    with TestClient(api.app) as c:
        yield c


def _wait(client, exp_id, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f"/api/experiments/{exp_id}").json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("experiment did not finish")


def test_api_experiments_flow(client, tmp_db, monkeypatch):
    up = make_bars([1.0] * 25 + [1.1, 1.2, 1.3, 1.4, 1.5], start="2026-10-07", spread=0.0)
    sd = up.index[24].strftime("%Y-%m-%d")
    _scan_with_universe(tmp_db, sd, [
        {"ticker": "UPP", "name": "Up", "close": 1.0, "score": 80, "verdict": "STRONG",
         "risk_flags": [{"code": "SUB_DOLLAR", "severity": "low", "text": "x"}]},
    ], [("UPP", 1.0), ("OTH", 2.0)])
    monkeypatch.setattr(tracking, "last_settled_session", lambda now=None: up.index[26].date())
    monkeypatch.setattr(experiments, "_default_fetch",
                        lambda t, s: {x: up[up.index <= up.index[26]] for x in t if x == "UPP"})

    d = client.get("/api/experiments/defaults").json()
    assert set(d) == {"backtest", "forward"} and "ranges" in d["backtest"] and "params" in d["forward"]
    assert d["backtest"]["defaults"] == d["backtest"]["params"] and d["backtest"]["ranges"]["top_n"]["max"] == 50
    assert client.post("/api/experiments", json={"kind": "bogus"}).status_code == 400
    assert client.post("/api/experiments", json={"kind": "backtest", "params": {"top_n": 0}}).status_code == 400
    assert client.post("/api/experiments", content=b"{nope", headers={"content-type": "application/json"}
                       ).status_code == 400
    assert client.post("/api/experiments", json={"kind": "forward", "params": {"scan_id": 999}}).status_code == 400

    monkeypatch.setattr(scan.STATE, "running", True)
    assert client.post("/api/experiments", json={"kind": "forward"}).status_code == 409
    monkeypatch.setattr(scan.STATE, "running", False)
    monkeypatch.setattr(experiments.RUNNER, "running", True)
    assert client.post("/api/experiments", json={"kind": "forward"}).status_code == 409
    monkeypatch.setattr(experiments.RUNNER, "running", False)

    r = client.post("/api/experiments", json={"kind": "forward", "name": "my test", "params": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"id", "status"} and isinstance(body["id"], int)
    det = _wait(client, body["id"])
    assert det["status"] == "waiting" and det["name"] == "my test"
    lst = client.get("/api/experiments").json()
    assert set(lst[0]) == {"id", "kind", "name", "status", "created_at", "finished_at", "due_date", "params",
                           "progress", "message", "headline"}
    assert set(lst[0]["headline"]) == {"picks", "evaluated", "hit_rate", "baseline_hit_rate", "avg_return",
                                       "baseline_avg_return"}
    assert {"notes", "error", "summary", "picks", "picks_total"} <= set(det)
    G = {"count", "evaluated", "hit_rate", "avg_return", "median_return", "avg_max_gain", "avg_max_drawdown",
         "win_rate"}
    assert G <= set(det["summary"]["overall"]) and G <= set(det["summary"]["baseline"])
    assert all(G | {"verdict"} <= set(v) for v in det["summary"]["by_verdict"])
    assert {"date", "picks_avg_return", "baseline_avg_return", "cumulative_picks", "cumulative_baseline"} <= set(
        det["summary"]["equity_curve"][0])
    p = det["picks"][0]
    assert set(p) == {"signal_date", "ticker", "name", "score", "verdict", "entry_date", "entry_price", "exit_date",
                      "exit_price", "return", "max_gain", "max_drawdown", "hit", "status", "risk_flags"}
    assert p["status"] == "partial" and p["hit"] is None and p["risk_flags"] == ["SUB_DOLLAR"]
    assert det["summary"]["baseline"]["count"] == 2  # OTH has no bars -> pending, still counted

    # evaluate endpoint: window now complete
    monkeypatch.setattr(tracking, "last_settled_session", lambda now=None: up.index[29].date())
    monkeypatch.setattr(experiments, "_default_fetch", lambda t, s: {x: up for x in t if x == "UPP"})
    ev = client.post(f"/api/experiments/{body['id']}/evaluate").json()
    assert ev["picks"][0]["status"] == "final" and ev["picks"][0]["hit"] is True
    assert ev["status"] == "completed" and ev["progress"] == 1.0

    # delete: 409 while running, then OK, then 404
    db.update_experiment(body["id"], tmp_db, status="running")
    assert client.delete(f"/api/experiments/{body['id']}").status_code == 409
    db.update_experiment(body["id"], tmp_db, status="completed")
    assert client.delete(f"/api/experiments/{body['id']}").status_code == 200
    assert client.get(f"/api/experiments/{body['id']}").status_code == 404
    assert client.post("/api/experiments/999/evaluate").status_code == 404


def test_api_backtest_runs_in_background_and_nan_is_null(client, tmp_db, monkeypatch):
    bars = universe(8)

    def fake_run(exp_id, db_path=None, **kw):
        idx = next(iter(bars.values())).index
        p = db.get_experiment(exp_id, db_path)["params"]
        assert p["top_n"] == 2
        # widen the synthetic range regardless of the default dates
        db.update_experiment(exp_id, db_path, params={**p, "start_date": idx[80].strftime("%Y-%m-%d"),
                                                      "end_date": idx[159].strftime("%Y-%m-%d")})
        return orig(exp_id, db_path, bars=bars, names={})

    orig = experiments.run_backtest_experiment
    monkeypatch.setattr(experiments, "run_backtest_experiment", fake_run)
    r = client.post("/api/experiments", json={"kind": "backtest", "params": {"top_n": 2, "include_filings": False}})
    assert r.status_code == 200, r.text
    det = _wait(client, r.json()["id"])
    assert det["status"] == "completed", det
    assert det["picks_total"] == len(det["picks"]) > 0
    # windows at the very end of the range are partial -> null hit, not NaN
    assert any(p["hit"] is None for p in det["picks"]) and any(p["status"] == "partial" for p in det["picks"])
    assert det["summary"]["by_month"] and det["headline"]["picks"] == det["picks_total"]


def test_api_failed_experiment_reports_error(client, tmp_db, monkeypatch):
    def boom(exp_id, db_path=None, **kw):
        raise RuntimeError("no network")

    monkeypatch.setattr(experiments, "run_backtest_experiment", boom)
    r = client.post("/api/experiments", json={"kind": "backtest", "params": {}})
    det = _wait(client, r.json()["id"])
    assert det["status"] == "failed" and "no network" in det["error"] and det["progress"] is None
    assert det["summary"]["overall"]["count"] == 0 and det["picks"] == []

import pandas as pd
import pytest

from tradealert import db, tracking


def test_forward_returns_start_next_trading_day(bars_factory):
    # scan day is index 20 (close 1.0); following closes 1.1, 1.2, ... ; highs 2% above
    closes = [1.0] * 21 + [1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0]
    df = bars_factory(closes)
    scan_date = df.index[20].strftime("%Y-%m-%d")
    out = tracking.compute_forward_outcome(df, scan_date, stored_entry=1.0)
    assert out["entry_close"] == 1.0
    assert out["ret_1d"] == pytest.approx(0.1)
    assert out["ret_3d"] == pytest.approx(0.3)
    assert out["ret_5d"] == pytest.approx(0.5)
    assert out["ret_10d"] == pytest.approx(1.0)
    # max high of next 5 bars = high of bar 5 = max(open 1.4, close 1.5) * 1.02
    assert out["max_gain_5d"] == pytest.approx(1.5 * 1.02 - 1)
    # lowest low of next 5 bars = bar 1: min(open 1.0, close 1.1) * 0.98
    assert out["max_drawdown_5d"] == pytest.approx(1.0 * 0.98 - 1)
    assert out["complete"] == 1


def test_scan_day_bar_is_not_part_of_the_window(bars_factory):
    closes = [1.0] * 20 + [1.0, 0.9, 0.9, 0.9, 0.9, 0.9]
    highs = [1.0] * 20 + [5.0] + [0.9] * 5  # huge spike ON the scan day must be ignored
    df = bars_factory(closes, highs=highs, lows=[0.9] * 26)
    out = tracking.compute_forward_outcome(df, df.index[20].strftime("%Y-%m-%d"), 1.0)
    assert out["max_gain_5d"] == pytest.approx(-0.1)


def test_partial_windows_are_null(bars_factory):
    df = bars_factory([1.0] * 21 + [1.1, 1.2])
    out = tracking.compute_forward_outcome(df, df.index[20].strftime("%Y-%m-%d"), 1.0)
    assert out["ret_1d"] == pytest.approx(0.1)
    assert out["ret_3d"] is None and out["ret_5d"] is None and out["max_gain_5d"] is None
    assert out["complete"] == 0 and out["bars_available"] == 2


def test_entry_reread_from_adjusted_series_handles_reverse_split(bars_factory):
    # After a 1:10 reverse split the whole series is re-adjusted (x10). Stored entry 0.5
    # would show a fake +900%; the re-read entry (5.0) gives the true +10%.
    df = bars_factory([5.0] * 21 + [5.5] * 5)
    out = tracking.compute_forward_outcome(df, df.index[20].strftime("%Y-%m-%d"), 0.5)
    assert out["entry_close"] == 5.0
    assert out["ret_1d"] == pytest.approx(0.1)


def test_no_bars_returns_nulls():
    out = tracking.compute_forward_outcome(None, "2026-01-02", 1.0)
    assert out["ret_1d"] is None and out["bars_available"] == 0


def _insert_scan(path, scan_date, picks):
    sid = db.create_scan({}, path)
    db.update_scan(sid, path, status="completed", scan_date=scan_date, finished_at="2026-01-01T00:00:00+00:00",
                   candidates_count=len(picks))
    db.insert_candidates(sid, scan_date, picks, path)
    return sid


def test_update_outcomes_and_report(tmp_db, bars_factory):
    df_up = bars_factory([1.0] * 21 + [1.1, 1.2, 1.3, 1.4, 1.5])   # +53% max gain -> hit
    df_flat = bars_factory([2.0] * 21 + [2.0] * 5, spread=0.01)
    scan_date = df_up.index[20].strftime("%Y-%m-%d")
    _insert_scan(tmp_db, scan_date, [
        {"ticker": "UPP", "close": 1.0, "score": 80, "verdict": "STRONG"},
        {"ticker": "FLAT", "close": 2.0, "score": 70, "verdict": "STRONG"},
        {"ticker": "BAD", "close": 1.0, "score": 10, "verdict": "AVOID"},
    ])
    calls = []

    def fake_fetch(tickers, start):
        calls.append((sorted(tickers), start))
        return {"FLAT": df_flat}

    res = tracking.update_outcomes(tmp_db, fetch=fake_fetch, known_bars={"UPP": df_up})
    assert res["pending"] == 3
    assert calls and calls[0][0] == ["BAD", "FLAT"]  # UPP served from known bars
    rep = tracking.performance_report(tmp_db)
    strong = next(v for v in rep["by_verdict"] if v["verdict"] == "STRONG")
    assert strong["count"] == 2
    assert strong["hit_rate"] == pytest.approx(0.5)
    assert strong["avg_close_return_5d"] == pytest.approx((0.5 + 0.0) / 2)
    avoid = next(v for v in rep["by_verdict"] if v["verdict"] == "AVOID")
    assert avoid["count"] == 0 and avoid["hit_rate"] is None and avoid["pending"] == 1
    upp = next(p for p in rep["picks"] if p["ticker"] == "UPP")
    assert upp["ret_5d"] == pytest.approx(0.5) and upp["ret_10d"] is None
    assert rep["hit_threshold_pct"] == 20.0


def test_last_settled_session():
    import datetime as dt

    from tradealert.utils import ET

    fri_midday = dt.datetime(2026, 11, 13, 12, 0, tzinfo=ET)
    fri_evening = dt.datetime(2026, 11, 13, 18, 0, tzinfo=ET)
    sunday = dt.datetime(2026, 11, 15, 10, 0, tzinfo=ET)
    mon_morning = dt.datetime(2026, 11, 16, 9, 0, tzinfo=ET)
    assert tracking.last_settled_session(fri_midday) == dt.date(2026, 11, 12)
    assert tracking.last_settled_session(fri_evening) == dt.date(2026, 11, 13)
    assert tracking.last_settled_session(sunday) == dt.date(2026, 11, 13)
    assert tracking.last_settled_session(mon_morning) == dt.date(2026, 11, 13)

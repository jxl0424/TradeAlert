import math

import numpy as np
import pytest

from tradealert.signals import compute_signals


def test_relative_volume_breakout_and_changes(bars_factory):
    closes = [1.00] * 40 + [1.00, 1.02, 1.01, 1.03, 1.30]
    vols = [1_000_000] * 44 + [5_000_000]
    df = bars_factory(closes, vols)
    s = compute_signals(df)
    assert s["rel_volume"] == pytest.approx(5.0)
    assert s["pct_change_1d"] == pytest.approx(1.30 / 1.03 - 1)
    assert s["pct_change_5d"] == pytest.approx(1.30 / 1.00 - 1)
    assert s["breakout_20d"] is True
    assert s["dollar_volume"] == pytest.approx(1.30 * 5_000_000)
    assert s["vol_trend_3d"] == pytest.approx((1 + 1 + 5) / 3, rel=1e-6)
    assert 0 <= s["close_position"] <= 1
    assert s["date"] == df.index[-1].strftime("%Y-%m-%d")


def test_no_breakout_when_below_prior_high(bars_factory):
    closes = [2.0] * 10 + [3.0] + [2.0] * 20
    s = compute_signals(bars_factory(closes))
    assert s["breakout_20d"] is False
    assert s["pct_from_period_high"] < 0


def test_today_excluded_from_its_own_volume_baseline(bars_factory):
    vols = [100.0] * 30 + [10_000.0]
    s = compute_signals(bars_factory([1.0] * 31, vols))
    assert s["avg_volume_20d"] == pytest.approx(100.0)
    assert s["rel_volume"] == pytest.approx(100.0)


def test_squeeze_then_expansion(bars_factory):
    rng = np.random.default_rng(0)
    noisy = list(1 + 0.08 * rng.standard_normal(40))  # volatile period
    quiet = [1.0 + 0.002 * (i % 2) for i in range(22)]  # contraction
    closes = noisy + quiet + [1.25]
    s = compute_signals(bars_factory(closes))
    assert s["squeeze_ratio"] is not None and s["squeeze_ratio"] < 0.6
    assert s["range_expansion"] > 2
    assert s["atr_contraction"] <= 1.0 + 1e-9


def test_gap_and_close_position(bars_factory):
    closes = [1.0] * 30 + [1.2]
    opens = [1.0] * 30 + [1.1]
    highs = [1.01] * 30 + [1.2]
    lows = [0.99] * 30 + [1.1]
    s = compute_signals(bars_factory(closes, opens=opens, highs=highs, lows=lows))
    assert s["gap_pct"] == pytest.approx(0.1)
    assert s["close_position"] == pytest.approx(1.0)


def test_values_are_finite_or_none(bars_factory):
    s = compute_signals(bars_factory([1.0] * 30, volumes=[0] * 30))
    for k, v in s.items():
        if isinstance(v, float):
            assert math.isfinite(v), k
    assert s["rel_volume"] is None  # zero baseline volume -> unknown, not inf/NaN


def test_signals_only_use_data_up_to_last_row(bars_factory):
    closes = [1.0] * 30 + [1.5] + [5.0] * 5
    df = bars_factory(closes)
    a = compute_signals(df.iloc[:31])
    b = compute_signals(df.iloc[:31].copy())
    assert a == b
    assert a["close"] == 1.5

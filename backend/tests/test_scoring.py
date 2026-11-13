import pytest

from tradealert import config
from tradealert.scoring import (
    catalyst_component,
    merge_flags,
    score_candidate,
    technical_score,
    verdict_for,
)
from tradealert.signals import compute_signals

QUIET = {"close": 2.0, "open": 2.0, "rel_volume": 0.9, "vol_trend_3d": 1.0, "pct_change_1d": 0.0,
         "pct_change_5d": -0.01, "breakout_20d": False, "close_position": 0.5, "squeeze_ratio": 1.2,
         "range_expansion": 0.9, "gap_pct": 0.0, "avg_dollar_volume_20d": 2e6, "volume": 1e6}
HOT = {"close": 2.4, "open": 2.1, "rel_volume": 9.0, "vol_trend_3d": 3.5, "pct_change_1d": 0.2,
       "pct_change_5d": 0.35, "breakout_20d": True, "breakout_pct": 0.1, "close_position": 0.95,
       "squeeze_ratio": 0.45, "range_expansion": 3.2, "gap_pct": 0.05, "avg_dollar_volume_20d": 3e6,
       "volume": 12e6}
SMALL_FLOAT = {"float_shares": 8e6, "short_pct_float": 0.25, "market_cap": 40e6}
CATALYST = {"catalysts": [{"form": "8-K", "filed_at": "2026-11-12", "description": "8-K: 8.01 Other events",
                           "age_days": 1}], "risk_flags": []}


def test_quiet_stock_is_weak():
    r = score_candidate(QUIET)
    assert r["score"] < config.VERDICT_WATCH
    assert r["verdict"] == "WEAK"


def test_hot_stock_scores_high_and_components_add_up():
    r = score_candidate(HOT, SMALL_FLOAT, CATALYST, news_recent_count=3, news_total=4, has_sec_data=True)
    assert r["verdict"] == "STRONG"
    pos = sum(c["points"] for c in r["components"] if c["name"] != "risk_penalty")
    pen = next(c for c in r["components"] if c["name"] == "risk_penalty")["points"]
    assert r["score"] == pytest.approx(min(100, max(0, round(pos + pen, 1))), abs=0.11)
    for c in r["components"]:
        assert set(c) == {"name", "points", "max_points", "reason"}
        if c["name"] != "risk_penalty":
            assert 0 <= c["points"] <= c["max_points"]
    assert sum(config.WEIGHTS.values()) == 100
    assert r["top_reasons"]


def test_fresh_dilution_forces_avoid():
    dil = {"catalysts": [], "risk_flags": [{"code": "DILUTION_PROSPECTUS", "severity": "high", "text": "424B5"}]}
    r = score_candidate(HOT, SMALL_FLOAT, dil, news_recent_count=3, news_total=3, has_sec_data=True)
    assert r["verdict"] == "AVOID"
    assert r["risk_flags"][0]["code"] == "DILUTION_PROSPECTUS"
    assert r["top_reasons"][0].startswith("RISK:")
    clean = score_candidate(HOT, SMALL_FLOAT, {"catalysts": [], "risk_flags": []}, 3, 3, has_sec_data=True)
    assert clean["score"] - r["score"] == pytest.approx(config.RISK_PENALTY["high"], abs=0.11)


def test_extended_move_flag_and_penalty():
    hot = {**HOT, "pct_change_1d": 0.8}
    r = score_candidate(hot)
    codes = {f["code"] for f in r["risk_flags"]}
    assert "EXTENDED_MOVE" in codes


def test_financial_status_and_reverse_split_flags():
    f = {"reverse_split": {"date": "2026-11-01", "ratio": 0.1, "days_ago": 12}}
    r = score_candidate(HOT, f, financial_status="E")
    codes = {fl["code"]: fl["severity"] for fl in r["risk_flags"]}
    assert codes["REVERSE_SPLIT_RECENT"] == "high"
    assert codes["NASDAQ_STATUS_E"] == "high"
    assert r["verdict"] == "AVOID"


def test_missing_fundamentals_are_tolerated():
    r = score_candidate(HOT, {"float_shares": None, "short_pct_float": None, "market_cap": None})
    fs = next(c for c in r["components"] if c["name"] == "float_short")
    assert fs["points"] == 0
    assert "unknown" in fs["reason"]


def test_verdict_thresholds():
    assert verdict_for(config.VERDICT_STRONG, []) == "STRONG"
    assert verdict_for(config.VERDICT_WATCH, []) == "WATCH"
    assert verdict_for(config.VERDICT_WATCH - 0.1, []) == "WEAK"
    assert verdict_for(99, [{"code": "X", "severity": "high", "text": ""}]) == "AVOID"
    assert verdict_for(99, [{"code": "X", "severity": "medium", "text": ""}]) == "STRONG"


def test_merge_flags_keeps_highest_severity():
    out = merge_flags([{"code": "A", "severity": "low", "text": "a"}],
                      [{"code": "A", "severity": "high", "text": "b"}, {"code": "B", "severity": "medium", "text": "c"}])
    assert [f["code"] for f in out] == ["A", "B"]
    assert out[0]["severity"] == "high"


def test_catalyst_decays_with_age():
    fresh = catalyst_component({"catalysts": [{"form": "8-K", "age_days": 1, "description": "x"}]}, 0, 0)
    old = catalyst_component({"catalysts": [{"form": "8-K", "age_days": 20, "description": "x"}]}, 0, 0)
    assert fresh["points"] > old["points"] == 0


def test_technical_score_ranks_spike_above_quiet(bars_factory):
    quiet = compute_signals(bars_factory([1.0] * 45))
    spike = compute_signals(bars_factory([1.0] * 44 + [1.25], [1e6] * 44 + [8e6]))
    assert technical_score(spike) > technical_score(quiet)


def test_volume_on_down_day_gets_reduced_credit():
    up = score_candidate({**HOT})
    down = score_candidate({**HOT, "pct_change_1d": -0.15})
    vol_up = next(c for c in up["components"] if c["name"] == "volume")["points"]
    vol_down = next(c for c in down["components"] if c["name"] == "volume")["points"]
    assert vol_down == pytest.approx(vol_up * config.DOWN_DAY_VOLUME_FACTOR, abs=0.11)
    setup_down = next(c for c in down["components"] if c["name"] == "setup")
    assert "downside" in setup_down["reason"]

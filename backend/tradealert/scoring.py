"""Transparent scoring: named components (points/max/reason), risk flags, verdict.

The final score is sum(component points) - risk penalties, clamped to 0..100.
Weights and thresholds live in `config`.
"""
from __future__ import annotations

import math

from . import config

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _comp(name: str, points: float, max_points: float, reasons: list[str]) -> dict:
    return {
        "name": name,
        "points": round(min(points, max_points), 1),
        "max_points": max_points,
        "reason": "; ".join(r for r in reasons if r) or "no signal",
    }


# --------------------------------------------------------------------------- technical components
def volume_component(s: dict) -> dict:
    mx = config.WEIGHTS["volume"]
    rv, vt = s.get("rel_volume"), s.get("vol_trend_3d")
    pts, reasons = 0.0, []
    if rv is not None:
        f = _clamp(math.log(rv) / math.log(8)) if rv > 1 else 0.0  # 8x avg volume = full marks
        pts += 0.8 * mx * f
        reasons.append(f"volume {rv:.1f}x its 20-day average")
    else:
        reasons.append("relative volume unavailable")
    if vt is not None:
        pts += 0.2 * mx * _clamp((vt - 1) / 2)  # 3-day volume 3x baseline = full marks
        if vt > 1.3:
            reasons.append(f"3-day volume {vt:.1f}x prior baseline (building)")
    p1 = s.get("pct_change_1d")
    if p1 is not None and p1 <= config.DOWN_DAY_PCT and pts > 0:
        # Heavy volume on a falling day is distribution, not accumulation.
        pts *= config.DOWN_DAY_VOLUME_FACTOR
        reasons.append(f"but on a down day ({_pct(p1)}): volume credit reduced")
    return _comp("volume", pts, mx, reasons)


def momentum_component(s: dict) -> dict:
    mx = config.WEIGHTS["momentum"]
    p1, p5 = s.get("pct_change_1d"), s.get("pct_change_5d")
    pts, reasons = 0.0, []
    if p1 is not None:
        pts += 0.40 * mx * _clamp(p1 / 0.15)
        reasons.append(f"1-day {_pct(p1)}")
    if p5 is not None:
        pts += 0.20 * mx * _clamp(p5 / 0.30)
        reasons.append(f"5-day {_pct(p5)}")
    if s.get("breakout_20d"):
        pts += 0.24 * mx
        reasons.append(f"closed above 20-day high ({_pct(s.get('breakout_pct'))} over)")
    cp = s.get("close_position")
    if cp is not None and (p1 or 0) > 0:
        pts += 0.16 * mx * _clamp(cp)
        if cp >= 0.8:
            reasons.append("closed near the high of the day")
        elif cp <= 0.3:
            reasons.append("faded into the close (closed near day low)")
    return _comp("momentum", pts, mx, reasons)


def setup_component(s: dict) -> dict:
    mx = config.WEIGHTS["setup"]
    pts, reasons = 0.0, []
    sq = s.get("squeeze_ratio")
    if sq is not None:
        f = _clamp((1 - sq) / 0.5)  # prior band width <= half its median = full marks
        pts += 0.47 * mx * f
        if f > 0.2:
            reasons.append(f"volatility squeeze before today (Bollinger width {sq:.2f}x median)")
    rex = s.get("range_expansion")
    up_day = (s.get("pct_change_1d") or 0) > 0
    if rex is not None and up_day:  # only upside expansion counts
        pts += 0.33 * mx * _clamp((rex - 1) / 2)
        if rex >= 1.5:
            reasons.append(f"today's range {rex:.1f}x the 14-day ATR (upside expansion)")
    elif rex is not None and rex >= 1.5:
        reasons.append(f"range expanded {rex:.1f}x ATR but to the downside (no credit)")
    gap = s.get("gap_pct")
    if gap is not None and gap >= 0.03:
        if s.get("close", 0) >= s.get("open", 0):
            pts += 0.20 * mx
            reasons.append(f"gapped up {_pct(gap)} and held above the open")
        else:
            reasons.append(f"gapped up {_pct(gap)} but closed below the open")
    return _comp("setup", pts, mx, reasons)


def technical_score(s: dict) -> float:
    """Preliminary score used to build the shortlist (bars only, no network)."""
    return sum(c["points"] for c in (volume_component(s), momentum_component(s), setup_component(s)))


# --------------------------------------------------------------------------- enrichment components
def float_short_component(f: dict, s: dict) -> dict:
    mx = config.WEIGHTS["float_short"]
    pts, reasons = 0.0, []
    fl = f.get("float_shares")
    if fl is None:
        fl = f.get("shares_outstanding")
        if fl is not None:
            reasons.append("float unknown, using shares outstanding")
    if fl is not None and fl > 0:
        if fl < 10e6:
            pts += 0.53 * mx
        elif fl < 20e6:
            pts += 0.40 * mx
        elif fl < 50e6:
            pts += 0.20 * mx
        reasons.append(f"float {fl / 1e6:.1f}M shares")
        vol = s.get("volume")
        if vol:
            rot = vol / fl
            if rot >= 1:
                pts += 0.20 * mx
                reasons.append(f"traded {rot:.1f}x the float today")
            elif rot >= 0.3:
                pts += 0.10 * mx
                reasons.append(f"traded {rot:.0%} of the float today")
    else:
        reasons.append("float unknown")
    sp = f.get("short_pct_float")
    if sp is not None:
        if sp >= 0.20:
            pts += 0.27 * mx
        elif sp >= 0.10:
            pts += 0.13 * mx
        reasons.append(f"short interest {sp:.0%} of float")
    return _comp("float_short", pts, mx, reasons)


def catalyst_component(filings_summary: dict | None, news_recent_count: int, news_total: int) -> dict:
    mx = config.WEIGHTS["catalyst"]
    pts, reasons = 0.0, []
    best = 0.0
    for c in (filings_summary or {}).get("catalysts", []):
        age = c.get("age_days", 999)
        weight = 1.0 if age <= 3 else 0.6 if age <= 7 else 0.25 if age <= 14 else 0.0
        base = 0.5 * mx if c.get("form", "").startswith("8-K") else 0.25 * mx
        if base * weight > best:
            best = base * weight
        if weight > 0:
            reasons.append(f"{c.get('form')} {c.get('filed_at')}: {c.get('description')}")
    pts += best
    if news_recent_count:
        pts += 0.5 * mx * _clamp(news_recent_count / 3)
        reasons.append(f"{news_recent_count} news item(s) in the last 3 days")
    elif news_total:
        reasons.append(f"{news_total} news item(s) this week, none in the last 3 days")
    else:
        reasons.append("no recent news or catalyst filings found")
    return _comp("catalyst", pts, mx, reasons[:4])


# --------------------------------------------------------------------------- risk
def technical_risk_flags(s: dict) -> list[dict]:
    flags = []
    p1, p5 = s.get("pct_change_1d"), s.get("pct_change_5d")
    if (p1 is not None and p1 >= config.EXTENDED_1D) or (p5 is not None and p5 >= config.EXTENDED_5D):
        flags.append({"code": "EXTENDED_MOVE", "severity": "medium",
                      "text": f"Already extended (1d {_pct(p1)}, 5d {_pct(p5)}): chasing risk, frequent fades"})
    close = s.get("close")
    if close is not None and close < 1.0:
        flags.append({"code": "SUB_DOLLAR", "severity": "low",
                      "text": "Trades under $1: listing-compliance and reverse-split risk"})
    adv = s.get("avg_dollar_volume_20d")
    if adv is not None and adv < 250_000:
        flags.append({"code": "LOW_LIQUIDITY", "severity": "low",
                      "text": f"Thin liquidity: 20-day avg dollar volume ${adv:,.0f}"})
    return flags


def fundamental_risk_flags(f: dict, financial_status: str | None, has_sec_data: bool | None) -> list[dict]:
    from .universe import FINANCIAL_STATUS

    flags = []
    if financial_status and financial_status in FINANCIAL_STATUS:
        sev = "medium" if financial_status == "D" else "high"
        flags.append({"code": f"NASDAQ_STATUS_{financial_status}", "severity": sev,
                      "text": f"NASDAQ financial status: {FINANCIAL_STATUS[financial_status]}"})
    rs = f.get("reverse_split")
    if rs:
        sev = "high" if rs["days_ago"] <= 30 else "medium"
        flags.append({"code": "REVERSE_SPLIT_RECENT", "severity": sev,
                      "text": f"Reverse split {rs['date']} (ratio {rs['ratio']:.4g}, {rs['days_ago']}d ago)"})
    mc = f.get("market_cap")
    if mc is not None and mc < 5e6:
        flags.append({"code": "MICRO_CAP", "severity": "low", "text": f"Market cap only ${mc / 1e6:.1f}M"})
    if has_sec_data is False:
        flags.append({"code": "NO_SEC_DATA", "severity": "low",
                      "text": "No SEC filer found for this ticker: filings/dilution could not be checked"})
    return flags


def merge_flags(*groups: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for g in groups:
        for fl in g or []:
            cur = best.get(fl["code"])
            if cur is None or SEVERITY_RANK[fl["severity"]] > SEVERITY_RANK[cur["severity"]]:
                best[fl["code"]] = fl
    return sorted(best.values(), key=lambda x: (-SEVERITY_RANK[x["severity"]], x["code"]))


def penalty_component(flags: list[dict]) -> dict:
    total = sum(config.RISK_PENALTY.get(f["severity"], 0) for f in flags)
    total = min(total, config.MAX_TOTAL_PENALTY)
    if flags:
        counts = {s: sum(1 for f in flags if f["severity"] == s) for s in ("high", "medium", "low")}
        reason = ", ".join(f"{n} {s}" for s, n in counts.items() if n) + " severity risk flag(s)"
    else:
        reason = "no risk flags"
    return {"name": "risk_penalty", "points": -float(total), "max_points": config.MAX_TOTAL_PENALTY,
            "reason": reason}


def verdict_for(score: float, flags: list[dict]) -> str:
    if any(f["severity"] in config.AVOID_SEVERITIES for f in flags):
        return "AVOID"
    if score >= config.VERDICT_STRONG:
        return "STRONG"
    if score >= config.VERDICT_WATCH:
        return "WATCH"
    return "WEAK"


def score_candidate(
    signals: dict,
    fundamentals: dict | None = None,
    filings_summary: dict | None = None,
    news_recent_count: int = 0,
    news_total: int = 0,
    financial_status: str | None = None,
    has_sec_data: bool | None = None,
) -> dict:
    f = fundamentals or {}
    comps = [
        volume_component(signals),
        momentum_component(signals),
        setup_component(signals),
        float_short_component(f, signals),
        catalyst_component(filings_summary, news_recent_count, news_total),
    ]
    flags = merge_flags(
        technical_risk_flags(signals),
        fundamental_risk_flags(f, financial_status, has_sec_data),
        (filings_summary or {}).get("risk_flags", []),
    )
    pen = penalty_component(flags)
    raw = sum(c["points"] for c in comps)
    score = round(_clamp(raw + pen["points"], 0, 100), 1)
    verdict = verdict_for(score, flags)
    ranked = sorted((c for c in comps if c["points"] > 0), key=lambda c: c["points"] / c["max_points"], reverse=True)
    top_reasons = [f"{c['name'].replace('_', '/')}: {c['reason']}" for c in ranked[:3]]
    high = [fl["text"] for fl in flags if fl["severity"] == "high"]
    if high:
        top_reasons.insert(0, f"RISK: {high[0]}")
    return {
        "score": score,
        "raw_score": round(raw, 1),
        "verdict": verdict,
        "components": comps + [pen],
        "risk_flags": flags,
        "top_reasons": top_reasons,
    }

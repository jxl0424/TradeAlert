"""yfinance fundamentals for shortlisted tickers. Every field may be missing."""
from __future__ import annotations

import datetime as dt
import time

from .utils import fnum, log


def parse_info(info: dict | None) -> dict:
    info = info or {}
    short_pct = fnum(info.get("shortPercentOfFloat"))
    # yfinance reports a fraction (0.12); guard against occasional percent values (12.0)
    if short_pct is not None and short_pct > 1.5:
        short_pct = short_pct / 100.0
    return {
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "float_shares": fnum(info.get("floatShares")),
        "shares_outstanding": fnum(info.get("sharesOutstanding")),
        "market_cap": fnum(info.get("marketCap")),
        "short_pct_float": short_pct,
        "short_ratio_days": fnum(info.get("shortRatio")),
        "fifty_two_week_high": fnum(info.get("fiftyTwoWeekHigh")),
        "fifty_two_week_low": fnum(info.get("fiftyTwoWeekLow")),
        "held_pct_insiders": fnum(info.get("heldPercentInsiders")),
        "country": info.get("country"),
    }


def recent_reverse_split(splits, asof: dt.date, days: int = 120) -> dict | None:
    """Most recent reverse split (ratio < 1) within `days` of asof, from Ticker.splits."""
    try:
        if splits is None or len(splits) == 0:
            return None
        for ts, ratio in list(splits.items())[::-1]:
            d = ts.date() if hasattr(ts, "date") else ts
            if d > asof:
                continue
            if (asof - d).days > days:
                break
            if ratio and float(ratio) < 1:
                return {"date": d.isoformat(), "ratio": float(ratio), "days_ago": (asof - d).days}
    except Exception:  # noqa: BLE001
        return None
    return None


def fetch_fundamentals(ticker: str, asof: dt.date, retries: int = 2) -> dict:
    import yfinance as yf

    data: dict = {}
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
            # A throttled/failed lookup often returns a stub dict instead of raising.
            if len(info) < 20 and info.get("marketCap") is None and attempt < retries:
                raise RuntimeError(f"sparse info ({len(info)} fields), likely throttled")
            data = parse_info(info)
            try:
                data["reverse_split"] = recent_reverse_split(t.splits, asof)
            except Exception:  # noqa: BLE001
                data["reverse_split"] = None
            return data
        except Exception as exc:  # noqa: BLE001
            log.info("fundamentals failed for %s (attempt %d): %s", ticker, attempt + 1, exc)
            time.sleep(2 * (attempt + 1))
    return parse_info({}) | {"reverse_split": None}

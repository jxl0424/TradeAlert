"""Company news: yfinance (no key) plus optional Finnhub company-news (FINNHUB_API_KEY)."""
from __future__ import annotations

import datetime as dt
import os

from . import config
from .utils import RateLimiter, http_session, log

_finnhub_limiter = RateLimiter(1.0)  # free tier: 60/min


def _parse_ts(value) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat()
        s = str(value).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def parse_yf_news(items: list) -> list[dict]:
    """Handle both the new (`content` nested) and legacy flat yfinance news shapes."""
    out = []
    for raw in items or []:
        c = raw.get("content", raw) if isinstance(raw, dict) else {}
        if not isinstance(c, dict):
            continue
        headline = c.get("title")
        if not headline:
            continue
        url = None
        for key in ("canonicalUrl", "clickThroughUrl"):
            v = c.get(key)
            if isinstance(v, dict) and v.get("url"):
                url = v["url"]
                break
        url = url or c.get("link")
        provider = c.get("provider")
        source = provider.get("displayName") if isinstance(provider, dict) else c.get("publisher")
        published = _parse_ts(c.get("pubDate") or c.get("displayTime") or c.get("providerPublishTime"))
        out.append({
            "headline": headline.strip(),
            "source": source,
            "url": url,
            "published_at": published,
            "summary": (c.get("summary") or c.get("description") or None),
        })
    return out


def yfinance_news(ticker: str) -> list[dict]:
    import yfinance as yf

    try:
        return parse_yf_news(yf.Ticker(ticker).news or [])
    except Exception as exc:  # noqa: BLE001
        log.info("yfinance news failed for %s: %s", ticker, exc)
        return []


def finnhub_news(ticker: str, asof: dt.date, days: int) -> list[dict]:
    key = os.environ.get("FINNHUB_API_KEY", config.FINNHUB_API_KEY).strip()
    if not key:
        return []
    _finnhub_limiter.wait()
    try:
        r = http_session().get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker, "from": (asof - dt.timedelta(days=days)).isoformat(),
                    "to": asof.isoformat(), "token": key},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json() or []
    except Exception as exc:  # noqa: BLE001
        log.info("finnhub news failed for %s: %s", ticker, type(exc).__name__)
        return []
    return [{
        "headline": (d.get("headline") or "").strip(),
        "source": d.get("source"),
        "url": d.get("url"),
        "published_at": _parse_ts(d.get("datetime")),
        "summary": d.get("summary") or None,
    } for d in data if d.get("headline")]


def news_for_ticker(ticker: str, asof: dt.date, days: int | None = None, limit: int = 15) -> list[dict]:
    """Merged, de-duplicated news, newest first. Items after the scan day's end are dropped."""
    days = days or config.NEWS_LOOKBACK_DAYS
    items = yfinance_news(ticker) + finnhub_news(ticker, asof, days)
    start = dt.datetime.combine(asof - dt.timedelta(days=days), dt.time(), tzinfo=dt.timezone.utc)
    # allow news up to the next morning (UTC) of the as-of day: after-close headlines count
    end = dt.datetime.combine(asof + dt.timedelta(days=1), dt.time(12), tzinfo=dt.timezone.utc)
    seen, out = set(), []
    for it in items:
        key = it["headline"].lower()[:80]
        if key in seen:
            continue
        ts = it.get("published_at")
        if ts:
            t = dt.datetime.fromisoformat(ts)
            if t < start or t > end:
                continue
        seen.add(key)
        out.append(it)
    out.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    return out[:limit]


def recent_news_count(news: list[dict], asof: dt.date, days: int = 3) -> int:
    cutoff = dt.datetime.combine(asof - dt.timedelta(days=days), dt.time(), tzinfo=dt.timezone.utc)
    n = 0
    for it in news:
        ts = it.get("published_at")
        if ts and dt.datetime.fromisoformat(ts) >= cutoff:
            n += 1
    return n

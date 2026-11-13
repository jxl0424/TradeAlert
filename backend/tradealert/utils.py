"""Small shared helpers: JSON-safe values, HTTP session, rate limiting, market clock."""
from __future__ import annotations

import datetime as dt
import logging
import math
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("tradealert")

ET = ZoneInfo("America/New_York")


def clean(value: Any) -> Any:
    """Recursively convert numpy/pandas scalars to Python and NaN/inf to None."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, bool):
        return value
    # numpy bool
    if type(value).__name__ == "bool_":
        return bool(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        try:
            return clean(value.item())
        except (ValueError, TypeError):
            pass
    if hasattr(value, "isoformat"):  # pandas Timestamp
        return value.isoformat()
    return value


def fnum(value: Any) -> float | None:
    """Coerce to a finite float or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def http_session(user_agent: str | None = None) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
    s.headers["User-Agent"] = user_agent or "TradeAlert/0.1 (personal research tool)"
    s.headers["Accept-Encoding"] = "gzip, deflate"
    return s


class RateLimiter:
    """Thread-safe minimum-interval limiter (requests per second)."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


def now_et() -> dt.datetime:
    return dt.datetime.now(tz=ET)


def utc_now_iso() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0).isoformat()


def session_key(now: dt.datetime | None = None) -> str:
    """Cache key for end-of-day data: ET date plus whether today's session is settled."""
    from . import config

    now = now or now_et()
    hour = now.hour + now.minute / 60.0
    settled = now.weekday() >= 5 or hour >= config.MARKET_CLOSE_SETTLE_HOUR
    return f"{now.date().isoformat()}{'' if settled else '-intraday'}"


def is_incomplete_bar_date(bar_date: dt.date, now: dt.datetime | None = None) -> bool:
    """True when `bar_date` is today's (ET) session and the session hasn't settled yet."""
    from . import config

    now = now or now_et()
    if bar_date != now.date():
        return False
    hour = now.hour + now.minute / 60.0
    return now.weekday() < 5 and hour < config.MARKET_CLOSE_SETTLE_HOUR

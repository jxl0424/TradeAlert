"""All tunable settings live here: paths, filters, score weights, verdict thresholds.

Values can be overridden through environment variables (or a `.env` file in the
project root). Weights/thresholds are plain module constants so they are easy to
read and edit in one place.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # optional: load <project root>/.env if python-dotenv is installed
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else default


# --------------------------------------------------------------------------- paths
DATA_DIR = Path(_env_str("TRADEALERT_DATA_DIR", str(PROJECT_ROOT / "data")))
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = Path(_env_str("TRADEALERT_DB_PATH", str(DATA_DIR / "tradealert.db")))
FRONTEND_DIR = Path(_env_str("TRADEALERT_FRONTEND_DIR", str(PROJECT_ROOT / "frontend")))

# --------------------------------------------------------------------------- keys / identity
SEC_USER_AGENT = _env_str("SEC_USER_AGENT", "")  # e.g. "TradeAlert research tool you@example.com"
FINNHUB_API_KEY = _env_str("FINNHUB_API_KEY", "")
ANTHROPIC_API_KEY = _env_str("ANTHROPIC_API_KEY", "")
AI_MODEL = _env_str("TRADEALERT_AI_MODEL", "claude-haiku-5-5")
AI_TOP_N = _env_int("TRADEALERT_AI_TOP_N", 10)
AI_MAX_TOKENS = _env_int("TRADEALERT_AI_MAX_TOKENS", 2000)
AI_EFFORT = _env_str("TRADEALERT_AI_EFFORT", "low")

# --------------------------------------------------------------------------- universe / price filters
MIN_PRICE = _env_float("TRADEALERT_MIN_PRICE", 0.10)
MAX_PRICE = _env_float("TRADEALERT_MAX_PRICE", 5.00)
MIN_AVG_DOLLAR_VOLUME = _env_float("TRADEALERT_MIN_AVG_DOLLAR_VOLUME", 100_000)
MIN_BARS = 25  # need at least this many daily bars for the signals
PRICE_PERIOD = "3mo"
PRICE_CHUNK_SIZE = _env_int("TRADEALERT_PRICE_CHUNK_SIZE", 150)
PRICE_MAX_RETRIES = 4
PRICE_BACKOFF_SECONDS = 5.0
PRICE_PAUSE_BETWEEN_CHUNKS = _env_float("TRADEALERT_PRICE_PAUSE", 0.5)
# Treat a bar dated "today" (US/Eastern) as incomplete until this time (ET, 24h).
MARKET_CLOSE_SETTLE_HOUR = 16.5

SHORTLIST_SIZE = _env_int("TRADEALERT_SHORTLIST_SIZE", 40)
ENRICH_WORKERS = 4
FILINGS_LOOKBACK_DAYS = 30
NEWS_LOOKBACK_DAYS = 7
SEC_MAX_REQ_PER_SEC = 8.0  # SEC fair-access limit is 10/s; stay under it

# --------------------------------------------------------------------------- score weights
# Each component's maximum points. Sum of maxima = 100 before penalties.
WEIGHTS = {
    "volume": 25,      # relative volume today + multi-day volume trend
    "momentum": 25,    # 1d/5d change, 20d breakout, close near high of day
    "setup": 15,       # squeeze (range contraction) before today's expansion, gap hold
    "float_short": 15, # small float, short interest, float rotation
    "catalyst": 20,    # fresh 8-K catalyst items, recent news flow
}
# Points subtracted per risk flag by severity.
RISK_PENALTY = {"high": 25, "medium": 10, "low": 3}
MAX_TOTAL_PENALTY = 60

# Verdict thresholds on the final 0-100 score.
VERDICT_STRONG = 65
VERDICT_WATCH = 45
# Any risk flag with one of these severities forces verdict AVOID.
AVOID_SEVERITIES = {"high"}

# Volume on a day that closed down at least this much only earns this fraction of credit.
DOWN_DAY_PCT = -0.02
DOWN_DAY_VOLUME_FACTOR = 0.4

# Momentum guardrail: a move this big is flagged as "already extended".
EXTENDED_1D = 0.50
EXTENDED_5D = 1.00

# --------------------------------------------------------------------------- tracking
HIT_THRESHOLD_PCT = _env_float("TRADEALERT_HIT_THRESHOLD_PCT", 20.0)  # max-high gain within 5d
FORWARD_WINDOWS = (1, 3, 5, 10)


def ai_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY).strip())


def sec_user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", SEC_USER_AGENT).strip()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

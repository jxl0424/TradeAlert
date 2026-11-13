"""Tradable US common-stock universe from the NASDAQ Trader symbol directories (cached daily)."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from . import config
from .utils import http_session, log, now_et

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

NASDAQ_TIERS = {"Q": "NASDAQ GS", "G": "NASDAQ GM", "S": "NASDAQ CM"}
OTHER_EXCHANGES = {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX", "F": "TXSE"}

# NASDAQ "Financial Status" codes that indicate trouble.
FINANCIAL_STATUS = {
    "D": "Deficient: below continued listing standards",
    "E": "Delinquent: late with SEC filings",
    "Q": "Bankrupt",
    "G": "Deficient and bankrupt",
    "H": "Deficient and delinquent",
    "J": "Delinquent and bankrupt",
    "K": "Deficient, delinquent and bankrupt",
}

_EXCLUDE_NAME = re.compile(
    r"\b(warrants?|rights?|units?|preferred|preference|notes? due|senior notes|debentures?|"
    r"subordinated|trust preferred|depositary shares,? each representing a 1/)\b|\d+(\.\d+)?%",
    re.IGNORECASE,
)
_PARTNERSHIP = re.compile(r"\b(L\.?P\.?|limited partnership|partners)\b", re.IGNORECASE)
_SYMBOL_OK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


@dataclass
class Security:
    ticker: str          # yfinance-style symbol (class shares use '-')
    symbol: str          # exchange symbol as listed
    name: str
    exchange: str
    financial_status: str | None = None  # NASDAQ-listed only, e.g. "D"


def is_excluded_name(name: str) -> bool:
    m = _EXCLUDE_NAME.search(name)
    if not m:
        return False
    # "XYZ Partners L.P. Common Units" are ordinary equity, keep them.
    if m.group(0).lower().startswith("unit") and _PARTNERSHIP.search(name):
        return bool(re.search(r"warrant|right|preferred|notes", name, re.IGNORECASE))
    return True


def parse_nasdaq_listed(text: str) -> list[Security]:
    out: list[Security] = []
    lines = text.splitlines()
    if not lines:
        return out
    header = lines[0].split("|")
    idx = {h: i for i, h in enumerate(header)}
    for line in lines[1:]:
        if line.startswith("File Creation Time") or not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < len(header):
            continue
        sym, name = parts[idx["Symbol"]].strip(), parts[idx["Security Name"]].strip()
        if parts[idx["Test Issue"]] == "Y" or parts[idx["ETF"]] == "Y":
            continue
        if "NextShares" in idx and parts[idx["NextShares"]] == "Y":
            continue
        if not _SYMBOL_OK.match(sym) or is_excluded_name(name):
            continue
        status = parts[idx["Financial Status"]].strip() or None
        out.append(Security(
            ticker=sym.replace(".", "-"), symbol=sym, name=name,
            exchange=NASDAQ_TIERS.get(parts[idx["Market Category"]], "NASDAQ"),
            financial_status=status if status and status != "N" else None,
        ))
    return out


def parse_other_listed(text: str) -> list[Security]:
    out: list[Security] = []
    lines = text.splitlines()
    if not lines:
        return out
    header = lines[0].split("|")
    idx = {h: i for i, h in enumerate(header)}
    for line in lines[1:]:
        if line.startswith("File Creation Time") or not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < len(header):
            continue
        sym, name = parts[idx["ACT Symbol"]].strip(), parts[idx["Security Name"]].strip()
        if parts[idx["Test Issue"]] == "Y" or parts[idx["ETF"]] == "Y":
            continue
        if "$" in sym or not _SYMBOL_OK.match(sym) or is_excluded_name(name):
            continue
        out.append(Security(
            ticker=sym.replace(".", "-"), symbol=sym, name=name,
            exchange=OTHER_EXCHANGES.get(parts[idx["Exchange"]], parts[idx["Exchange"]]),
        ))
    return out


def load_universe(force: bool = False) -> list[Security]:
    """Return the filtered universe, using a once-per-day on-disk cache."""
    config.ensure_dirs()
    cache = config.CACHE_DIR / f"universe_{now_et().date().isoformat()}.json"
    if cache.exists() and not force:
        try:
            return [Security(**d) for d in json.loads(cache.read_text(encoding="utf-8"))]
        except (ValueError, TypeError):
            log.warning("universe cache unreadable, refetching")
    s = http_session()
    nl = s.get(NASDAQ_LISTED_URL, timeout=30)
    nl.raise_for_status()
    ol = s.get(OTHER_LISTED_URL, timeout=30)
    ol.raise_for_status()
    secs = parse_nasdaq_listed(nl.text) + parse_other_listed(ol.text)
    seen: set[str] = set()
    unique = []
    for sec in secs:
        if sec.ticker not in seen:
            seen.add(sec.ticker)
            unique.append(sec)
    unique.sort(key=lambda x: x.ticker)
    if len(unique) < 1000:
        raise RuntimeError(f"universe suspiciously small ({len(unique)} symbols); not caching")
    cache.write_text(json.dumps([asdict(x) for x in unique]), encoding="utf-8")
    for old in config.CACHE_DIR.glob("universe_*.json"):
        if old != cache:
            old.unlink(missing_ok=True)
    return unique

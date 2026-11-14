"""Daily OHLCV bars from yfinance: chunked batch download, retry/backoff, on-disk cache."""
from __future__ import annotations

import hashlib
import io
import contextlib
import pickle
import time
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from . import config
from .utils import is_incomplete_bar_date, log, session_key

BarMap = dict[str, pd.DataFrame]
OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def _normalize(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Keep OHLCV, date index (naive), drop empty rows and an incomplete 'today' bar."""
    if df is None or df.empty:
        return None
    cols = [c for c in OHLCV if c in df.columns]
    if len(cols) < 5:
        return None
    df = df[cols].copy()
    df = df.dropna(subset=["Close"])
    if df.empty:
        return None
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["Volume"] = df["Volume"].fillna(0)
    for c in ("Open", "High", "Low"):
        df[c] = df[c].fillna(df["Close"])
    if len(df) and is_incomplete_bar_date(df.index[-1].date()):
        df = df.iloc[:-1]
    return df if not df.empty else None


def split_download(data: pd.DataFrame | None, tickers: list[str]) -> BarMap:
    """Split a yfinance multi-ticker frame into {ticker: frame}."""
    out: BarMap = {}
    if data is None or data.empty:
        return out
    if isinstance(data.columns, pd.MultiIndex):
        level0 = set(data.columns.get_level_values(0))
        for t in tickers:
            if t in level0:
                norm = _normalize(data[t])
                if norm is not None:
                    out[t] = norm
    elif len(tickers) == 1:
        norm = _normalize(data)
        if norm is not None:
            out[tickers[0]] = norm
    return out


def _yf_download(tickers: list[str], period: str | None, start: str | None) -> tuple[BarMap, dict]:
    import yfinance as yf
    from yfinance import shared as yf_shared

    kwargs = dict(
        tickers=tickers, interval="1d", auto_adjust=False, actions=False, group_by="ticker",
        threads=True, progress=False, timeout=20, multi_level_index=True,
    )
    if start:
        kwargs["start"] = start
    else:
        kwargs["period"] = period or config.PRICE_PERIOD
    # yfinance prints per-ticker failures to stdout/stderr; keep the console readable.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        data = yf.download(**kwargs)
    errors = dict(getattr(yf_shared, "_ERRORS", {}) or {})
    return split_download(data, tickers), errors


def _is_rate_limit(msg: str) -> bool:
    m = msg.lower()
    return "rate" in m or "too many" in m or "429" in m


def _cache_dir(tag: str, root: str = "bars") -> Path:
    d = config.CACHE_DIR / root / session_key() / tag
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_cache(d: Path) -> dict[str, pd.DataFrame | None]:
    cached: dict[str, pd.DataFrame | None] = {}
    for f in sorted(d.glob("chunk_*.pkl")):
        try:
            with f.open("rb") as fh:
                cached.update(pickle.load(fh))
        except Exception:  # noqa: BLE001 - corrupt cache file: ignore and refetch
            log.warning("ignoring unreadable cache file %s", f)
    return cached


def prune_old_cache(keep: int = 3, root_name: str = "bars") -> None:
    root = config.CACHE_DIR / root_name
    if not root.exists():
        return
    dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)
    for old in dirs[:-keep]:
        for f in old.rglob("*"):
            if f.is_file():
                f.unlink(missing_ok=True)
        for sub in sorted(old.rglob("*"), reverse=True):
            if sub.is_dir():
                sub.rmdir()
        old.rmdir()


def download_bars(
    tickers: Iterable[str],
    period: str | None = None,
    start: str | None = None,
    use_cache: bool = True,
    progress: Callable[[int, int], None] | None = None,
    chunk_size: int | None = None,
    cache_root: str = "bars",
) -> BarMap:
    """Download daily bars for many tickers. Returns {ticker: DataFrame[Open..Volume]}.

    Tickers with no data are simply absent from the result. Results are cached per
    trading session so a rerun the same day only fetches what is missing.
    """
    tickers = sorted(set(tickers))
    chunk_size = chunk_size or config.PRICE_CHUNK_SIZE
    tag = f"start-{start}" if start else f"period-{period or config.PRICE_PERIOD}"
    cache_dir = _cache_dir(tag, cache_root) if use_cache else None
    cached = _load_cache(cache_dir) if cache_dir else {}
    result: BarMap = {t: df for t, df in cached.items() if t in tickers and df is not None}
    todo = [t for t in tickers if t not in cached]
    total_chunks = max(1, -(-len(todo) // chunk_size))
    log.info("bars: %d cached, %d to download in %d chunks", len(tickers) - len(todo), len(todo), total_chunks)

    for i in range(0, len(todo), chunk_size):
        chunk = todo[i:i + chunk_size]
        got: BarMap = {}
        pending = list(chunk)
        unresolved: list[str] = []
        for attempt in range(config.PRICE_MAX_RETRIES):
            try:
                part, errors = _yf_download(pending, period, start)
            except Exception as exc:  # noqa: BLE001 - network/yfinance hiccup: back off and retry
                log.warning("chunk download error (attempt %d): %s", attempt + 1, exc)
                part, errors = {}, {t: str(exc) for t in pending}
            got.update(part)
            rate_limited = [t for t in pending if t not in part and _is_rate_limit(str(errors.get(t, "")))]
            all_failed = bool(pending) and not part
            if not rate_limited and not all_failed:
                unresolved = []
                break
            pending = rate_limited or pending
            unresolved = list(pending)
            if attempt + 1 < config.PRICE_MAX_RETRIES:
                wait = config.PRICE_BACKOFF_SECONDS * (2 ** attempt)
                log.warning("bars: %d tickers rate-limited/failed, retrying in %.0fs", len(pending), wait)
                time.sleep(wait)
        if unresolved:
            log.warning("bars: giving up on %d tickers this run (not cached as missing)", len(unresolved))
        # Cache misses too (delisted/no data) so reruns stay cheap -- but not transient failures.
        chunk_result: dict[str, pd.DataFrame | None] = {
            t: got.get(t) for t in chunk if t in got or t not in unresolved
        }
        result.update(got)
        if cache_dir is not None:
            name = hashlib.sha1(",".join(chunk).encode()).hexdigest()[:12]
            with (cache_dir / f"chunk_{name}.pkl").open("wb") as fh:
                pickle.dump(chunk_result, fh)
        if progress:
            progress(i // chunk_size + 1, total_chunks)
        if config.PRICE_PAUSE_BETWEEN_CHUNKS:
            time.sleep(config.PRICE_PAUSE_BETWEEN_CHUNKS)
    return result


LONG_CACHE_ROOT = "bars_long"


def download_long_history(
    tickers: Iterable[str],
    period: str = "2y",
    use_cache: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> BarMap:
    """Up to 2 years of daily bars for backtests.

    Same chunked/retried download as the live scan, but cached under a separate root
    (`cache/bars_long/<session>/period-2y`), so it never touches or prunes the live scan's
    3-month cache. Only the two most recent sessions are kept.
    """
    out = download_bars(tickers, period=period, use_cache=use_cache, progress=progress,
                        cache_root=LONG_CACHE_ROOT)
    prune_old_cache(keep=2, root_name=LONG_CACHE_ROOT)
    return out


def as_of_date(bars: BarMap) -> pd.Timestamp | None:
    """Most common last-bar date across the universe (= the trading day being scanned)."""
    if not bars:
        return None
    last = pd.Series([df.index[-1] for df in bars.values() if len(df)])
    return last.mode().max() if len(last) else None


def filter_tradeable(
    bars: BarMap,
    asof: pd.Timestamp | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_avg_dollar_volume: float | None = None,
    min_bars: int | None = None,
) -> BarMap:
    """Keep tickers whose last close is in the price band, with enough liquidity and history.

    Tickers whose most recent bar is not `asof` (halted / stale) are dropped.
    """
    lo = config.MIN_PRICE if min_price is None else min_price
    hi = config.MAX_PRICE if max_price is None else max_price
    min_adv = config.MIN_AVG_DOLLAR_VOLUME if min_avg_dollar_volume is None else min_avg_dollar_volume
    nmin = config.MIN_BARS if min_bars is None else min_bars
    out: BarMap = {}
    for t, df in bars.items():
        if df is None or len(df) < nmin:
            continue
        if asof is not None and df.index[-1] != asof:
            continue
        close = float(df["Close"].iloc[-1])
        if not (lo <= close <= hi):
            continue
        adv = float((df["Close"] * df["Volume"]).tail(20).mean())
        if adv < min_adv:
            continue
        out[t] = df
    return out


def bars_to_records(df: pd.DataFrame, limit: int = 90) -> list[dict]:
    recs = []
    for ts, row in df.tail(limit).iterrows():
        recs.append({
            "time": ts.strftime("%Y-%m-%d"),
            "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": float(row["Close"]),
            "volume": float(row["Volume"]),
        })
    return recs

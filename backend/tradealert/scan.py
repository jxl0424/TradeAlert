"""Daily scan pipeline + CLI.

    python -m tradealert.scan               # full universe
    python -m tradealert.scan --limit 300   # dev: random sample of the universe
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import ai_summary, config, db, filings, fundamentals, news, prices, scoring, signals, tracking, universe
from .utils import log, utc_now_iso


class ScanState:
    """In-process scan status shared with the API (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.scan_id: int | None = None
        self.stage = "idle"
        self.progress = 0.0
        self.message = ""

    def try_start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.scan_id = None
            self.stage, self.progress, self.message = "starting", 0.0, "Starting scan"
            return True

    def update(self, stage: str | None = None, progress: float | None = None, message: str | None = None,
               scan_id: int | None = None) -> None:
        with self._lock:
            if stage is not None:
                self.stage = stage
            if progress is not None:
                self.progress = max(0.0, min(1.0, progress))
            if message is not None:
                self.message = message
            if scan_id is not None:
                self.scan_id = scan_id
        if message:
            log.info("[%s %.0f%%] %s", self.stage, self.progress * 100, message)

    def finish(self, stage: str, message: str) -> None:
        with self._lock:
            self.running = False
            self.stage = stage
            self.progress = 1.0 if stage == "completed" else self.progress
            self.message = message

    def snapshot(self) -> dict:
        with self._lock:
            return {"running": self.running, "scan_id": self.scan_id, "stage": self.stage,
                    "progress": round(self.progress, 3), "message": self.message}


STATE = ScanState()


def _enrich_one(ticker: str, asof: dt.date, ticker_map: dict | None) -> dict:
    fund = fundamentals.fetch_fundamentals(ticker, asof)
    if ticker_map is not None:
        fil = filings.filings_for_ticker(ticker, asof, ticker_map)
    else:
        fil = {"cik": None, "filings": [], "risk_flags": [], "catalysts": [], "error": "SEC disabled"}
    nws = news.news_for_ticker(ticker, asof)
    return {"fundamentals": fund, "filings": fil, "news": nws}


def run_scan(limit: int | None = None, shortlist_size: int | None = None, ai_top_n: int | None = None,
             db_path=None, state: ScanState | None = None, seed: int | None = None,
             use_cache: bool = True) -> int:
    """Run one full scan synchronously. Returns the scan id."""
    state = state or STATE
    shortlist_size = shortlist_size or config.SHORTLIST_SIZE
    ai_top_n = config.AI_TOP_N if ai_top_n is None else ai_top_n
    db.init_db(db_path)
    params = {"limit": limit, "shortlist_size": shortlist_size, "min_price": config.MIN_PRICE,
              "max_price": config.MAX_PRICE, "min_avg_dollar_volume": config.MIN_AVG_DOLLAR_VOLUME,
              "ai_enabled": config.ai_enabled(), "ai_model": config.AI_MODEL if config.ai_enabled() else None}
    scan_id = db.create_scan(params, db_path)
    state.update(stage="universe", progress=0.01, message="Loading symbol universe", scan_id=scan_id)
    t0 = time.monotonic()
    warnings: list[str] = []
    try:
        # 1. universe ---------------------------------------------------------------
        secs = universe.load_universe()
        if limit:
            rng = random.Random(seed if seed is not None else 42)
            secs = rng.sample(secs, min(limit, len(secs)))
        sec_by_ticker = {s.ticker: s for s in secs}
        db.update_scan(scan_id, db_path, universe_size=len(secs))
        state.update(stage="prices", progress=0.05, message=f"Universe: {len(secs)} symbols; downloading bars")

        # 2. prices -------------------------------------------------------------------
        def on_chunk(done: int, total: int) -> None:
            state.update(progress=0.05 + 0.55 * done / total, message=f"Price chunks {done}/{total}")

        bars = prices.download_bars(sec_by_ticker.keys(), use_cache=use_cache, progress=on_chunk)
        asof_ts = prices.as_of_date(bars)
        if asof_ts is None:
            raise RuntimeError("no price data downloaded (network or Yahoo rate limit?)")
        asof = asof_ts.date()
        scan_date = asof.isoformat()
        filtered = prices.filter_tradeable(bars, asof=asof_ts)
        db.update_scan(scan_id, db_path, scan_date=scan_date, filtered_size=len(filtered))
        # Persist the filtered universe: forward experiments use it as their baseline.
        db.insert_scan_universe(scan_id, [(t, float(df["Close"].iloc[-1])) for t, df in filtered.items()], db_path)
        state.update(stage="signals", progress=0.62,
                     message=f"Bars for {len(bars)} symbols (as of {scan_date}); {len(filtered)} pass price/liquidity filters")

        # 3. signals + preliminary score -------------------------------------------------
        prelim = []
        for t, df in filtered.items():
            try:
                s = signals.compute_signals(df)
            except Exception as exc:  # noqa: BLE001
                log.warning("signals failed for %s: %s", t, exc)
                continue
            prelim.append((scoring.technical_score(s), t, s))
        prelim.sort(key=lambda x: (-x[0], x[1]))
        shortlist = prelim[:shortlist_size]
        state.update(stage="enrich", progress=0.66, message=f"Enriching top {len(shortlist)} candidates")

        # 4. enrichment --------------------------------------------------------------
        ticker_map = None
        if config.sec_user_agent():
            try:
                ticker_map = filings.load_ticker_map()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"SEC ticker map unavailable: {exc}")
        else:
            warnings.append("SEC_USER_AGENT not set: filings/dilution checks skipped")
        enriched: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=config.ENRICH_WORKERS) as pool:
            futs = {t: pool.submit(_enrich_one, t, asof, ticker_map) for _, t, _ in shortlist}
            for i, (t, fut) in enumerate(futs.items(), 1):
                try:
                    enriched[t] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("enrichment failed for %s: %s", t, exc)
                    enriched[t] = {"fundamentals": {}, "filings": {"filings": [], "risk_flags": [], "catalysts": []},
                                   "news": []}
                state.update(progress=0.66 + 0.2 * i / max(1, len(futs)), message=f"Enriched {i}/{len(futs)}: {t}")

        # 5. final score --------------------------------------------------------------
        candidates = []
        for tech, t, s in shortlist:
            e = enriched[t]
            fund, fil, nws = e["fundamentals"], e["filings"], e["news"]
            sec = sec_by_ticker.get(t)
            has_sec = None if ticker_map is None else fil.get("cik") is not None
            if fund.get("fifty_two_week_high"):
                s["pct_from_52w_high"] = s["close"] / fund["fifty_two_week_high"] - 1
            if fund.get("fifty_two_week_low"):
                s["pct_from_52w_low"] = s["close"] / fund["fifty_two_week_low"] - 1
            s["technical_score"] = round(tech, 1)
            s["news_count_3d"] = news.recent_news_count(nws, asof, 3)
            res = scoring.score_candidate(
                s, fund, fil, news_recent_count=s["news_count_3d"], news_total=len(nws),
                financial_status=sec.financial_status if sec else None, has_sec_data=has_sec,
            )
            candidates.append({
                "ticker": t,
                "name": fund.get("name") or (sec.name if sec else None),
                "exchange": sec.exchange if sec else None,
                "sector": fund.get("sector"),
                "close": s["close"],
                "pct_change_1d": s.get("pct_change_1d"),
                "pct_change_5d": s.get("pct_change_5d"),
                "rel_volume": s.get("rel_volume"),
                "dollar_volume": s.get("dollar_volume"),
                "market_cap": fund.get("market_cap"),
                "float_shares": fund.get("float_shares"),
                "short_pct_float": fund.get("short_pct_float"),
                "score": res["score"],
                "verdict": res["verdict"],
                "top_reasons": res["top_reasons"],
                "risk_flags": res["risk_flags"],
                "components": res["components"],
                "signals": {**s, "raw_score": res["raw_score"],
                            "shares_outstanding": fund.get("shares_outstanding"),
                            "industry": fund.get("industry"), "country": fund.get("country"),
                            "sec_cik": fil.get("cik"), "sec_error": fil.get("error"),
                            "nasdaq_financial_status": sec.financial_status if sec else None},
                "bars": prices.bars_to_records(filtered[t], 90),
                "news": [{k: n.get(k) for k in ("headline", "source", "url", "published_at")} for n in nws],
                "filings": [{k: f.get(k) for k in ("form", "filed_at", "description", "url", "category", "items")}
                            for f in fil.get("filings", [])],
                "_news_full": nws,
                "_filings_full": fil.get("filings", []),
                "scan_date": scan_date,
                "ai_summary": None,
            })
        candidates.sort(key=lambda c: (-c["score"], c["ticker"]))

        # 6. AI summaries (optional, explanation only) ----------------------------------
        if config.ai_enabled() and ai_top_n > 0:
            top = candidates[:ai_top_n]
            for i, c in enumerate(top, 1):
                state.update(stage="ai", progress=0.87 + 0.08 * i / len(top), message=f"AI summary {i}/{len(top)}: {c['ticker']}")
                ctx = {**c, "news": c["_news_full"], "filings": c["_filings_full"]}
                c["ai_summary"] = ai_summary.summarize_candidate(ctx)
        for c in candidates:
            c.pop("_news_full", None)
            c.pop("_filings_full", None)

        db.insert_candidates(scan_id, scan_date, candidates, db_path)

        # 7. tracking -----------------------------------------------------------------
        state.update(stage="tracking", progress=0.96, message="Updating forward outcomes of past picks")
        try:
            tr = tracking.update_outcomes(db_path, known_bars=bars)
            log.info("tracking: %s", tr)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"tracking update failed: {exc}")
        try:
            from . import experiments

            ev = experiments.evaluate_waiting(db_path, known_bars=bars)
            if ev:
                log.info("forward experiments evaluated: %s", ev)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"experiment evaluation failed: {exc}")

        elapsed = time.monotonic() - t0
        msg = (f"Scanned {len(secs)} symbols, {len(filtered)} passed filters, {len(candidates)} candidates "
               f"in {elapsed:.0f}s")
        if warnings:
            msg += " | warnings: " + "; ".join(warnings)
        db.update_scan(scan_id, db_path, status="completed", finished_at=utc_now_iso(),
                       candidates_count=len(candidates), message=msg)
        state.finish("completed", msg)
        prices.prune_old_cache()
        return scan_id
    except Exception as exc:
        log.exception("scan %s failed", scan_id)
        db.update_scan(scan_id, db_path, status="failed", finished_at=utc_now_iso(), message=str(exc))
        state.finish("failed", f"Scan failed: {exc}")
        raise


def start_background_scan(**kwargs) -> int | None:
    """Start a scan thread. Returns the scan id, or None if a scan is already running."""
    if not STATE.try_start():
        return None
    started = threading.Event()

    def target() -> None:
        try:
            run_scan(state=STATE, **kwargs)
        except Exception:  # noqa: BLE001 - already recorded in DB/state
            pass
        finally:
            started.set()

    th = threading.Thread(target=target, name="tradealert-scan", daemon=True)
    th.start()
    # wait briefly until the scan row exists so we can return its id
    for _ in range(200):
        if STATE.snapshot()["scan_id"] is not None or started.is_set():
            break
        time.sleep(0.01)
    return STATE.snapshot()["scan_id"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one TradeAlert scan.")
    ap.add_argument("--limit", type=int, default=None, help="dev: random sample of N universe symbols")
    ap.add_argument("--shortlist", type=int, default=None, help="number of candidates to enrich")
    ap.add_argument("--ai-top", type=int, default=None, help="AI summaries for top N (0 disables)")
    ap.add_argument("--seed", type=int, default=None, help="random seed for --limit sampling")
    ap.add_argument("--no-cache", action="store_true", help="ignore the on-disk price cache")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    STATE.try_start()
    try:
        scan_id = run_scan(limit=args.limit, shortlist_size=args.shortlist, ai_top_n=args.ai_top,
                           seed=args.seed, use_cache=not args.no_cache)
    except Exception as exc:  # noqa: BLE001
        print(f"Scan failed: {exc}")
        return 1
    scan = db.get_scan(scan_id)
    print(f"\nScan #{scan_id} as of {scan['scan_date']}: {scan['message']}")
    print(f"{'TICKER':<7} {'SCORE':>5}  {'VERDICT':<7} {'CLOSE':>7} {'1D':>7} {'RVOL':>6}  TOP RISK")
    for c in db.list_candidates(scan_id)[:15]:
        risk = c["risk_flags"][0]["code"] if c["risk_flags"] else ""
        p1 = f"{c['pct_change_1d'] * 100:+.1f}%" if c["pct_change_1d"] is not None else "n/a"
        rv = f"{c['rel_volume']:.1f}x" if c["rel_volume"] is not None else "n/a"
        print(f"{c['ticker']:<7} {c['score']:>5.1f}  {c['verdict']:<7} {c['close']:>7.3f} {p1:>7} {rv:>6}  {risk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

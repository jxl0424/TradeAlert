# TradeAlert

A personal, local-PC screener that runs once a day after the US close, scans US-listed
penny stocks ($0.10 to $5.00) for signs of a potential price spike, and shows a verdict
with the evidence behind it. **You** make the decision; the tool only shows the evidence.

> **Disclaimer.** TradeAlert is a screening aid, not financial advice. Penny stocks are
> highly volatile and frequently diluted, halted, reverse-split or delisted. Scores are
> heuristics built from free, delayed and sometimes incomplete data, and can be wrong.
> The optional AI summary can also be wrong. Check every candidate yourself and never
> trade money you cannot afford to lose.

## How it works

1. **Universe**: NASDAQ Trader symbol directories (`nasdaqlisted.txt`, `otherlisted.txt`).
   Test issues, ETFs, warrants, units, rights, preferreds and notes are excluded. Cached daily.
2. **Prices**: about 3 months of daily bars from yfinance, downloaded in chunks with
   retry/backoff and cached on disk per trading session, so a rerun the same day is cheap.
   Filters: last close between $0.10 and $5.00, 20-day average dollar volume of at least $100k.
   If you scan before the session has settled (16:30 ET), today's partial bar is dropped
   and the scan is "as of" the previous trading day.
3. **Signals** for every filtered stock: relative volume, 3-day volume trend, 1/5/20-day
   change, 20-day breakout, gap, close position in the day's range, ATR/Bollinger
   squeeze then expansion, RSI, distance from the 3-month (and 52-week) high and low.
4. **Shortlist** of the top 40 by a preliminary technical score. Only these are enriched:
   - yfinance fundamentals: float, shares outstanding, market cap, short % of float,
     sector, recent reverse splits.
   - SEC EDGAR filings from the last 30 days, classified as catalyst, dilution, risk
     or routine. **Dilution flags** (S-1/F-1, S-3/F-3, 424B*, EFFECT, 8-K item 3.02),
     late filings (NT 10-K/10-Q), delisting notices and reverse-split signals are the
     most important risk signals.
   - News from yfinance, plus Finnhub if you set `FINNHUB_API_KEY`.
5. **Score (0 to 100)**: a transparent weighted sum of the named components `volume` (25),
   `momentum` (25), `setup` (15), `float_short` (15) and `catalyst` (20), minus risk
   penalties (high 25, medium 10, low 3, capped at 60). Verdicts: `STRONG` at 65 or
   above, `WATCH` at 45 or above, otherwise `WEAK`. Any **high-severity** risk flag
   (for example a fresh 424B prospectus or S-1) forces `AVOID`. All weights and thresholds
   are in `backend/tradealert/config.py`.
6. **AI summary** (optional): when `ANTHROPIC_API_KEY` is set, Claude writes a short bull
   case, bear case, catalyst read and key risk for the top 10 candidates. It uses
   `claude-haiku-5-5` by default (`TRADEALERT_AI_MODEL`), makes one call per ticker with
   capped tokens, and only explains the evidence. It never changes the score. If the call
   fails, the summary is left empty and the scan carries on.
7. **Performance tracking**: every scan's candidates are stored with the scan-day close.
   Later scans fill in forward results for 1, 3, 5 and 10 trading days: the close return,
   the peak gain (highest high within 5 days) and the max drawdown (lowest low within 5
   days). Windows start on the next trading day, so there is no look-ahead. The dashboard
   aggregates these by verdict: count, mean and median return, and hit rate (the share of
   picks whose peak gain within 5 days reached 20%; the threshold is configurable).

## Setup on Windows

Requires Python 3.13 (from python.org; tick "Add python.exe to PATH").

```powershell
cd C:\path\to\TradeAlert
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env
notepad .env                           # set SEC_USER_AGENT (with YOUR email); optional API keys
```

On Linux or macOS, use `python3.13 -m venv .venv`, `source .venv/bin/activate` and `cp .env.example .env`.

`SEC_USER_AGENT` is needed for the SEC filing checks. SEC rejects requests that don't
send a descriptive User-Agent with a contact email (HTTP 403). Without it the scan still
runs, but dilution and filing checks are skipped, and the scan message says so.

## Run a scan

From the project root, with the venv active:

```powershell
python run.py scan                 # full universe, about 6,000 symbols
python run.py scan --limit 300     # dev/quick test: random sample of 300 symbols
python run.py scan --ai-top 0      # skip AI summaries for this run
```

These are the same as `cd backend` followed by `python -m tradealert.scan [--limit N]`.
The scan prints the top candidates and stores everything in `data/tradealert.db`.

## Open the dashboard

```powershell
python run.py serve                # http://127.0.0.1:8000
# or: uvicorn tradealert.api:app --app-dir backend --port 8000
```

The API is under `/api` (interactive docs at `/docs`). The frontend in `frontend/`
(or `frontend/dist/` if it has been built) is served at `/`, and unknown paths fall back
to `index.html`. If there is no frontend yet, `/` returns a short JSON note. You can also
start a scan from the dashboard (`POST /api/scans/run`) and follow it through
`GET /api/scans/status`.

### API summary

| Method and path | Purpose |
|---|---|
| `GET /api/health` | `{status, last_scan_at, ai_enabled}` |
| `GET /api/scans` | scans, newest first |
| `POST /api/scans/run` | start a background scan (409 if one is running); dev option `?limit=N` |
| `GET /api/scans/status` | `{running, scan_id, stage, progress, message}` |
| `GET /api/candidates?scan_id=&verdict=&min_score=` | candidate list for a scan (default: the latest completed) |
| `GET /api/candidates/{ticker}?scan_id=` | full evidence: components, signals, bars, news, filings, AI summary, history |
| `GET /api/performance` | forward-performance stats by verdict and per pick (`?refresh=true` refetches first) |
| `POST /api/performance/refresh` | refetch bars for past picks and update their outcomes |

Returns are fractions (0.25 means +25%). Missing numbers are `null`.

## Schedule it daily (Windows Task Scheduler)

Run the scan after the US close has settled (after 16:30 ET; for example 17:30 ET, or
23:30 CET). Adjust the paths and time (the time is in your PC's local time zone):

```powershell
schtasks /Create /TN "TradeAlert daily scan" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 23:30 ^
  /TR "\"C:\path\to\TradeAlert\.venv\Scripts\python.exe\" \"C:\path\to\TradeAlert\run.py\" scan"
```

You can do the same in the Task Scheduler GUI: Create Basic Task, Weekly, Mon to Fri,
action "Start a program". Set the program to `...\TradeAlert\.venv\Scripts\python.exe`,
the arguments to `run.py scan`, and "Start in" to `C:\path\to\TradeAlert`. Tick
"Run whether user is logged on or not" if you want it to run unattended. The `.env` file
is read from the project folder, so the scheduled task picks up your settings. A scan of
the full universe takes a few minutes, mostly spent downloading prices (see below).

The dashboard server does not need to be running for scheduled scans. Start it when you
want to look at the results.

## Data sources and limits

| Source | Used for | Limits / caveats |
|---|---|---|
| NASDAQ Trader symbol directories | universe | Free, updated daily. Listed stocks only (no OTC/pink sheets). |
| yfinance (Yahoo Finance, unofficial) | daily bars, fundamentals, news, splits | Unofficial and rate-limited (HTTP 429). The scan chunks downloads, backs off, retries and caches. Fields such as float and short interest are often missing or stale (short interest is published twice a month). Data is end of day, not real time. |
| SEC EDGAR (`data.sec.gov`) | filings in the last 30 days | Needs `SEC_USER_AGENT`. Fair-access limit of 10 requests/s (the tool uses at most 8/s). Only form types and 8-K item numbers are classified; documents are not read. Companies not in SEC's ticker map (some foreign issuers) get a `NO_SEC_DATA` flag. |
| Finnhub (optional) | extra company news | Free key, 60 calls/min. |
| Anthropic API (optional) | AI explanation | Paid per token. With the default Haiku model, top 10 only and capped `max_tokens`, a day costs a few cents at most. |

Other limits worth knowing:
- The **"52-week" distances** come from yfinance fundamentals (shortlist only). The
  universe-wide signals use the 3-month window that was downloaded.
- News relevance and tone are not judged. The catalyst score counts fresh 8-K catalyst
  items and recent headlines. Read the headlines and the AI summary to judge them.
- **Dilution detection** is based on form types. An S-3 shelf filed months ago, an ATM
  program that is still active, or warrant overhangs older than 30 days are not seen.
- Forward performance is measured on split-adjusted Yahoo bars. The entry price is
  re-read from the adjusted series, so a later reverse split doesn't show up as a fake
  gain. Tickers that get delisted stop updating, and their outcomes stay `null`.

## Development

```bash
pip install -r requirements.txt
python -m pytest            # unit and API tests, no network
```

Layout: `backend/tradealert/` (`config`, `db`, `universe`, `prices`, `signals`, `scoring`,
`fundamentals`, `filings`, `news`, `ai_summary`, `tracking`, `scan`, `api`) and
`backend/tests/`. Data and caches go to `data/` (git-ignored).

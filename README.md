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

## Experiments: are the recommendations any good?

An experiment tests the tool's picks against a **baseline**: every stock that passed the same
price/liquidity filter on the same days (the picks are part of it). The question an experiment
answers is not "did the picks go up?" but "did the picks do better than just picking any filtered
penny stock that day?". Penny stocks spike often, so a 15% hit rate means nothing unless the baseline
is clearly lower. Every experiment is stored permanently in `data/tradealert.db` and shown in the
dashboard; nothing deletes them except an explicit delete.

Two kinds:

- **Backtest** (history run). The scanner is replayed over a past date range on up to 2 years of
  daily bars (a separate long-history download, cached under `data/cache/bars_long/`, which does
  not touch the live scan cache). For each trading day D, only bars up to D are used: the live
  filter as of D, the same signals on the trailing 65 bars (the live 3-month window), the live
  top-40 technical shortlist, SEC filings replayed as of D (catalyst points, dilution/AVOID flags;
  a filing counts on D only if EDGAR accepted it by 16:30 ET), then the top N by score. Each pick
  is bought at D+1's open (or D's close) and held for the next `horizon_days` bars.
  The first full-universe run downloads about 6,000 symbols (roughly 10 to 15 minutes); the replay
  itself takes a minute or two.
- **Forward** (prediction, then wait). The picks of a completed scan (or a fresh scan) are frozen
  now, together with the full filtered universe of that scan as the baseline. It shows `waiting`
  until the window has passed, with mark-to-market results on the bars so far. It is evaluated
  automatically at the end of every scan, when the server starts, and on demand
  (`POST /api/experiments/{id}/evaluate` or `python run.py experiment evaluate`).

```powershell
python run.py experiment backtest                                  # last ~6 months, defaults
python run.py experiment backtest --start 2026-03-02 --end 2026-09-30 --horizon 5 --top-n 10
python run.py experiment forward                                   # from the latest scan
python run.py experiment forward --fresh-scan --verdicts STRONG,WATCH
python run.py experiment evaluate                                  # settle waiting forward tests
python run.py experiment list
python run.py export-site --out _site                              # static, view-only copy of the dashboard
```

### Reading the results

- `hit_rate`: share of picks whose highest high in the window reached the hit threshold
  (default +20%) over the entry price. `avg_return` / `median_return`: close at the end of the
  window vs entry. `win_rate`: share with a positive close return. `avg_max_drawdown`: average of
  the lowest low vs entry (how much pain you sat through).
- Compare every number with the **baseline** on the same days. `by_verdict` and `by_month` rows
  carry `baseline_hit_rate` / `baseline_avg_return` for the same signal days. `edge` is picks minus
  baseline. If STRONG picks do not beat the baseline clearly, and in more than one period, the
  score has no proven edge, whatever the absolute numbers look like.
- A high hit rate together with a negative average return is common for penny stocks: they spike
  intraday and fade. The hit rate assumes you sold at the high, which nobody does reliably.
- The `equity_curve` puts 1/horizon of the capital into each day's picks (equal weight) for the
  window, compounded. It is an illustration, not a tradable strategy (no costs, no slippage).
- While a forward experiment is waiting, return figures are mark-to-market (`basis: "partial"`)
  and `hit` stays `null` until the full window exists.

### Caveats (also stored in each experiment's notes)

- **Survivorship bias**: the backtest replays today's listed symbols only. Stocks that were
  delisted, went bankrupt or were acquired during the period are missing. This flatters both picks
  and baseline, more so for penny stocks.
- **Split-adjusted prices**: a stock that later did a 1:10 reverse split shows 10x its real price
  historically and may wrongly fail the $0.10-$5 filter, and forward splits the reverse. Which
  stocks looked like penny stocks back then is approximate.
- **Not available historically**: float and short interest (15 of 100 points), news (half of the
  catalyst points), NASDAQ status, reverse-split and market-cap flags. The backtest score is the
  available components (volume, momentum, setup, plus the SEC-filing half of catalyst) rescaled
  to 0-100, with the normal penalties and verdict thresholds. It is close to, but not the same as,
  the live score.
- **Fills**: buying at the next open is optimistic for illiquid names (spread, gaps, slippage),
  and no commissions are modelled. Free Yahoo data has occasional bad prints that can inflate
  the max gain.
- A forward experiment from one scan is a small sample. Look at many of them, or at a backtest.
- Exchange holidays are not modelled in the forward `due_date`; evaluation simply waits for bars.
- SEC filing history comes from EDGAR's "recent" block (the last ~1,000 filings per company),
  which covers two years for nearly every penny stock. It needs `SEC_USER_AGENT`; without it the
  backtest runs without filings and says so in its notes.

### Experiments API

| Method and path | Purpose |
|---|---|
| `GET /api/experiments/defaults` | default params and allowed ranges for both kinds (for the form) |
| `POST /api/experiments` | `{kind: "backtest"\|"forward", name?, params}` starts a background run and returns `{id, status}`. 400 bad params, 409 if a scan or experiment is computing |
| `GET /api/experiments` | newest first, with `status`, `progress`, `message`, `headline` |
| `GET /api/experiments/{id}` | adds `notes`, `error`, `summary` (overall, baseline, by_verdict, by_month, equity_curve) and `picks` (top 2,000 by score, plus `picks_total`) |
| `POST /api/experiments/{id}/evaluate` | re-evaluate a waiting forward experiment now |
| `DELETE /api/experiments/{id}` | delete (409 while running) |

Backtest params: `start_date`, `end_date`, `horizon_days` (5), `hit_threshold_pct` (20), `top_n` (10),
`min_score` (0), `entry` (`next_open` or `close`), `include_filings` (true). Forward params: `scan_id`
(default: the latest completed scan) or `run_fresh_scan`, `verdicts` (STRONG, WATCH), `min_score`,
`horizon_days`, `hit_threshold_pct`, `entry`.

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

## Free hosting on GitHub

TradeAlert can run entirely on GitHub for free: GitHub Actions does the daily scan and the
experiments, and GitHub Pages serves a **view-only** copy of the dashboard. Nothing runs on your PC.

How it works: the workflows in `.github/workflows/` keep the SQLite database on an orphan branch
called `data` (a single file, force-pushed as one commit each time, so the branch never grows; the
default branch is never touched). After every run `python run.py export-site --out _site` snapshots
every API response as static JSON next to the dashboard files and Pages publishes that folder.
The static dashboard has no Run scan, New experiment or Delete buttons; it shows "Start on GitHub"
links to the workflows instead.

### One-time setup (repository owner)

1. **Enable Pages**: Settings, Pages, Build and deployment, Source: **GitHub Actions**.
2. **Workflow permissions** (only if runs fail with a 403 when pushing the `data` branch): Settings,
   Actions, General, Workflow permissions, **Read and write permissions**. Also make sure Actions
   are enabled for the repository.
3. **Optional secrets and variable** (Settings, Secrets and variables, Actions). The workflows succeed
   without any of them.
   - Variable `SEC_USER_AGENT` (tab "Variables"), for example `TradeAlert research tool you@example.com`.
     SEC asks for a contact email; a generic fallback is used if you set nothing. A secret with the same
     name also works.
   - Secret `ANTHROPIC_API_KEY`: AI summaries for the top candidates (small paid usage).
   - Secret `FINNHUB_API_KEY`: extra company news.
4. **Run the first scan manually**: Actions tab, **Daily scan**, Run workflow. The first run
   creates the `data` branch and deploys the site; a full scan takes several minutes. After that it runs
   by itself on weekdays at 22:17 UTC (after the US close in both winter and summer time).
5. **Dashboard URL**: `https://<your-user>.github.io/<repo-name>/` (also shown on the deploy job and
   under Settings, Pages).

### Start an experiment

Actions tab, **Experiment**, Run workflow. Pick `backtest` or `forward` and fill in any of the optional
fields (blank means the CLI default; `start_date`, `end_date` and `top_n` apply to backtests only). A
backtest downloads about two years of prices and takes roughly 10 to 15 minutes. When it finishes the
site is redeployed with the new experiment. Forward experiments are settled by the daily scan once their
window has passed.

### Caveats

- **Everything is public** when the repository is public: the dashboard, the picks and the `data`
  branch database. Do not rely on this for anything private. Use a private repository (Pages from private
  repositories needs a paid plan) if that matters.
- Scheduled runs can start late or be skipped under GitHub load, and GitHub disables scheduled workflows
  after 60 days without repository activity (re-enable them in the Actions tab, or run one manually).
- Yahoo Finance may throttle GitHub's shared servers. The scan retries once after 10 minutes; if both
  fail, the run is marked failed and the previous data stays online.
- Only one workflow writes the database at a time (they queue), and GitHub keeps at most one waiting
  run per queue, so triggering many runs at once can drop the middle ones.
- The static site keeps candidate pages for the 30 most recent scans.
- Scheduled workflows run only from the default branch, so the `.github/workflows` files must be merged there.

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
`fundamentals`, `filings`, `news`, `ai_summary`, `tracking`, `scan`, `experiments`, `backtest`, `api`) and
`backend/tests/`. Data and caches go to `data/` (git-ignored).

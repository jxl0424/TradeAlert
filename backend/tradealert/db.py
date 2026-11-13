"""SQLite persistence (stdlib sqlite3). JSON blobs hold nested evidence."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config
from .utils import clean, utc_now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date TEXT,              -- as-of trading date of the bars used (YYYY-MM-DD)
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,        -- running | completed | failed
    universe_size INTEGER,
    filtered_size INTEGER,
    candidates_count INTEGER,
    message TEXT,
    params TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    scan_date TEXT,
    ticker TEXT NOT NULL,
    name TEXT,
    exchange TEXT,
    sector TEXT,
    close REAL,
    pct_change_1d REAL,
    pct_change_5d REAL,
    rel_volume REAL,
    dollar_volume REAL,
    market_cap REAL,
    float_shares REAL,
    short_pct_float REAL,
    score REAL,
    verdict TEXT,
    top_reasons TEXT,
    risk_flags TEXT,
    components TEXT,
    signals TEXT,
    bars TEXT,
    news TEXT,
    filings TEXT,
    ai_summary TEXT,
    UNIQUE(scan_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_candidates_ticker ON candidates(ticker);

CREATE TABLE IF NOT EXISTS outcomes (
    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    entry_close REAL,
    ret_1d REAL, ret_3d REAL, ret_5d REAL, ret_10d REAL,
    max_gain_5d REAL, max_drawdown_5d REAL,
    max_gain_10d REAL, max_drawdown_10d REAL,
    bars_available INTEGER,
    complete INTEGER DEFAULT 0,
    updated_at TEXT
);
"""

JSON_FIELDS = ("top_reasons", "risk_flags", "components", "signals", "bars", "news", "filings", "ai_summary")


def _path(db_path: Path | str | None) -> Path:
    return Path(db_path) if db_path else config.DB_PATH


@contextmanager
def connect(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    path = _path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(clean(value), separators=(",", ":"))


def loads(value: str | None) -> Any:
    return json.loads(value) if value else None


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in JSON_FIELDS:
        if f in d:
            d[f] = loads(d[f])
    return d


# --------------------------------------------------------------------------- scans
def create_scan(params: dict | None = None, db_path=None) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO scans (started_at, status, params) VALUES (?, 'running', ?)",
            (utc_now_iso(), dumps(params or {})),
        )
        return int(cur.lastrowid)


def update_scan(scan_id: int, db_path=None, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect(db_path) as conn:
        conn.execute(f"UPDATE scans SET {cols} WHERE id = ?", (*fields.values(), scan_id))


def mark_stale_running_scans(db_path=None, older_than_hours: float = 3.0) -> None:
    """Scans left 'running' by a crashed process are marked failed."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE scans SET status='failed', message='interrupted (process exited)' "
            "WHERE status='running' AND datetime(started_at) < datetime('now', ?)",
            (f"-{older_than_hours} hours",),
        )


SCAN_PUBLIC = ("id", "scan_date", "started_at", "finished_at", "status", "universe_size", "candidates_count")


def scan_public(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {k: row.get(k) for k in SCAN_PUBLIC}


def list_scans(db_path=None, limit: int = 200) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_scan(scan_id: int, db_path=None) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    return dict(row) if row else None


def latest_completed_scan(db_path=None) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM scans WHERE status='completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- candidates
CANDIDATE_COLUMNS = (
    "scan_id", "scan_date", "ticker", "name", "exchange", "sector", "close", "pct_change_1d",
    "pct_change_5d", "rel_volume", "dollar_volume", "market_cap", "float_shares",
    "short_pct_float", "score", "verdict", "top_reasons", "risk_flags", "components",
    "signals", "bars", "news", "filings", "ai_summary",
)


def insert_candidates(scan_id: int, scan_date: str, candidates: list[dict], db_path=None) -> None:
    rows = []
    for c in candidates:
        c = {**c, "scan_id": scan_id, "scan_date": scan_date}
        rows.append(tuple(
            dumps(c.get(col)) if col in JSON_FIELDS else clean(c.get(col)) for col in CANDIDATE_COLUMNS
        ))
    placeholders = ", ".join("?" for _ in CANDIDATE_COLUMNS)
    with connect(db_path) as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO candidates ({', '.join(CANDIDATE_COLUMNS)}) VALUES ({placeholders})",
            rows,
        )


def list_candidates(scan_id: int, verdict: str | None = None, min_score: float | None = None,
                    db_path=None) -> list[dict]:
    sql = ("SELECT id, ticker, name, exchange, sector, close, pct_change_1d, pct_change_5d, rel_volume, "
           "dollar_volume, market_cap, float_shares, short_pct_float, score, verdict, top_reasons, "
           "risk_flags FROM candidates WHERE scan_id = ?")
    args: list[Any] = [scan_id]
    if verdict:
        sql += " AND verdict = ?"
        args.append(verdict.upper())
    if min_score is not None:
        sql += " AND score >= ?"
        args.append(min_score)
    sql += " ORDER BY score DESC, ticker ASC"
    with connect(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [row_to_dict(r) for r in rows]


def get_candidate(scan_id: int, ticker: str, db_path=None) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM candidates WHERE scan_id = ? AND ticker = ?", (scan_id, ticker.upper())
        ).fetchone()
    return row_to_dict(row)


def ticker_history(ticker: str, before_scan_id: int, db_path=None) -> list[dict]:
    """Appearances on earlier trading days (latest scan per day), newest first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT c.scan_date, c.score, c.verdict FROM candidates c WHERE c.id IN ("
            "  SELECT MAX(c2.id) FROM candidates c2 JOIN scans s ON s.id = c2.scan_id "
            "  WHERE c2.ticker = ? AND s.status = 'completed' "
            "  AND c2.scan_date < (SELECT scan_date FROM scans WHERE id = ?) GROUP BY c2.scan_date) "
            "ORDER BY c.scan_date DESC",
            (ticker.upper(), before_scan_id),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- outcomes
def picks_needing_outcomes(db_path=None) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT c.id AS candidate_id, c.ticker, c.scan_date, c.close AS entry_close "
            "FROM candidates c JOIN scans s ON s.id = c.scan_id "
            "LEFT JOIN outcomes o ON o.candidate_id = c.id "
            "WHERE s.status = 'completed' AND c.scan_date IS NOT NULL "
            "AND (o.complete IS NULL OR o.complete = 0)"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_outcome(candidate_id: int, outcome: dict, db_path=None) -> None:
    cols = ("entry_close", "ret_1d", "ret_3d", "ret_5d", "ret_10d", "max_gain_5d", "max_drawdown_5d",
            "max_gain_10d", "max_drawdown_10d", "bars_available", "complete")
    values = [clean(outcome.get(c)) for c in cols]
    with connect(db_path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO outcomes (candidate_id, {', '.join(cols)}, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in cols)}, ?)",
            (candidate_id, *values, utc_now_iso()),
        )


def all_picks_with_outcomes(db_path=None) -> list[dict]:
    """Every stored pick (deduped per scan_date+ticker, latest scan wins) with outcomes."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT c.id AS candidate_id, c.scan_id, c.scan_date, c.ticker, c.verdict, c.score, "
            "c.close AS stored_close, o.entry_close, o.ret_1d, o.ret_3d, o.ret_5d, o.ret_10d, "
            "o.max_gain_5d, o.max_drawdown_5d "
            "FROM candidates c JOIN scans s ON s.id = c.scan_id "
            "LEFT JOIN outcomes o ON o.candidate_id = c.id "
            "WHERE s.status = 'completed' AND c.id IN ("
            "  SELECT MAX(c2.id) FROM candidates c2 JOIN scans s2 ON s2.id = c2.scan_id "
            "  WHERE s2.status='completed' GROUP BY c2.scan_date, c2.ticker) "
            "ORDER BY c.scan_date DESC, c.score DESC"
        ).fetchall()
    return [dict(r) for r in rows]

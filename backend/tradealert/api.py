"""FastAPI app: JSON API under /api, static frontend at / (index.html fallback).

    uvicorn tradealert.api:app --app-dir backend --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import config, db, scan, tracking
from .utils import clean

log = logging.getLogger("tradealert")


def _db_path():
    return config.DB_PATH


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    db.init_db(_db_path())
    db.mark_stale_running_scans(_db_path())
    yield


app = FastAPI(title="TradeAlert", version="0.1.0", lifespan=lifespan)


def _json(data, status_code: int = 200) -> JSONResponse:
    return JSONResponse(clean(data), status_code=status_code)


def _resolve_scan(scan_id: int | None) -> dict | None:
    if scan_id is not None:
        s = db.get_scan(scan_id, _db_path())
        if s is None:
            raise HTTPException(status_code=404, detail=f"scan {scan_id} not found")
        return s
    return db.latest_completed_scan(_db_path())


@app.get("/api/health")
def health():
    last = db.latest_completed_scan(_db_path())
    return _json({"status": "ok", "last_scan_at": last["finished_at"] if last else None,
                  "ai_enabled": config.ai_enabled()})


@app.get("/api/scans")
def scans():
    return _json([db.scan_public(s) for s in db.list_scans(_db_path())])


@app.post("/api/scans/run")
def run_scan_endpoint(limit: int | None = Query(default=None, ge=1, description="dev: sample N symbols")):
    scan_id = scan.start_background_scan(limit=limit, db_path=_db_path())
    if scan_id is None:
        snap = scan.STATE.snapshot()
        if snap["running"]:
            return _json({"detail": "a scan is already running", "scan_id": snap["scan_id"]}, 409)
        raise HTTPException(status_code=500, detail=snap["message"] or "scan failed to start")
    return _json({"scan_id": scan_id, "status": "running"})


@app.get("/api/scans/status")
def scan_status():
    return _json(scan.STATE.snapshot())


@app.get("/api/candidates")
def candidates(scan_id: int | None = None, verdict: str | None = None, min_score: float | None = None):
    s = _resolve_scan(scan_id)
    if s is None:
        return _json({"scan": None, "candidates": []})
    rows = db.list_candidates(s["id"], verdict=verdict, min_score=min_score, db_path=_db_path())
    for r in rows:
        r.pop("id", None)
    return _json({"scan": db.scan_public(s), "candidates": rows})


@app.get("/api/candidates/{ticker}")
def candidate_detail(ticker: str, scan_id: int | None = None):
    s = _resolve_scan(scan_id)
    if s is None:
        raise HTTPException(status_code=404, detail="no completed scans yet")
    c = db.get_candidate(s["id"], ticker, _db_path())
    if c is None:
        raise HTTPException(status_code=404, detail=f"{ticker.upper()} not in scan {s['id']}")
    for k in ("id", "scan_id"):
        c.pop(k, None)
    c["scan"] = db.scan_public(s)
    c["history"] = db.ticker_history(ticker, s["id"], _db_path())
    for k in ("components", "risk_flags", "top_reasons", "bars", "news", "filings"):
        c[k] = c.get(k) or []
    c["signals"] = c.get("signals") or {}
    return _json(c)


@app.get("/api/performance")
def performance(refresh: bool = False):
    if refresh:
        tracking.update_outcomes(_db_path())
    return _json(tracking.performance_report(_db_path()))


@app.post("/api/performance/refresh")
def performance_refresh():
    """Re-fetch bars for past picks and fill in forward outcomes (network)."""
    result = tracking.update_outcomes(_db_path())
    return _json({**result, **tracking.performance_report(_db_path())})


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def api_not_found(rest: str):
    return _json({"detail": f"unknown API route /api/{rest}"}, 404)


# --------------------------------------------------------------------------- static frontend
def _frontend_dir() -> Path:
    return config.FRONTEND_DIR


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str, request: Request):
    root = _frontend_dir()
    # Common build output folders: serve frontend/dist if it exists, else frontend/.
    for base in (root / "dist", root):
        if not base.is_dir():
            continue
        base_resolved = base.resolve()
        if path:
            target = (base / path).resolve()
            if target.is_file() and base_resolved in target.parents:
                return FileResponse(target)
            if Path(path).suffix and Path(path).suffix != ".html":
                return _json({"detail": f"static file not found: {path}"}, 404)
        index = base / "index.html"
        if index.is_file():
            return FileResponse(index)
    return _json({"detail": "frontend not built/installed; the API is available under /api",
                  "docs": "/docs"}, 404 if path else 200)

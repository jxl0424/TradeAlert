"""FastAPI app: JSON API under /api, static frontend at / (index.html fallback).

    uvicorn tradealert.api:app --app-dir backend --port 8000
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import config, db, experiments, scan, tracking
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
    db.mark_stale_running_experiments(_db_path())
    try:  # settle waiting forward experiments in the background (network)
        experiments.evaluate_waiting_background(_db_path())
    except Exception as exc:  # noqa: BLE001
        log.warning("startup experiment evaluation failed: %s", exc)
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


# --------------------------------------------------------------------------- experiments
@app.get("/api/experiments/defaults")
def experiment_defaults():
    return _json(experiments.defaults())


@app.get("/api/experiments")
def experiment_list():
    return _json([experiments.public_item(e) for e in db.list_experiments(_db_path())])


@app.post("/api/experiments")
async def experiment_create(request: Request):
    """Start an experiment in a background thread -> {id, status}. 400 bad params, 409 busy."""
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return _json({"detail": "body must be valid JSON"}, 400)
    if not isinstance(body, dict):
        return _json({"detail": "body must be a JSON object"}, 400)
    kind = body.get("kind")
    try:
        exp_id = experiments.start(kind, body.get("params"), name=body.get("name"), db_path=_db_path())
    except experiments.ParamError as exc:
        return _json({"detail": str(exc)}, 400)
    except experiments.Conflict as exc:
        return _json({"detail": str(exc)}, 409)
    exp = db.get_experiment(exp_id, _db_path())
    return _json({"id": exp_id, "status": exp["status"] if exp else "running"})


def _get_experiment_or_404(exp_id: int) -> dict:
    exp = db.get_experiment(exp_id, _db_path())
    if exp is None:
        raise HTTPException(status_code=404, detail=f"experiment {exp_id} not found")
    return exp


@app.get("/api/experiments/{exp_id}")
def experiment_detail(exp_id: int):
    return _json(experiments.public_detail(_get_experiment_or_404(exp_id), _db_path()))


@app.post("/api/experiments/{exp_id}/evaluate")
def experiment_evaluate(exp_id: int):
    exp = _get_experiment_or_404(exp_id)
    if exp["kind"] != "forward":
        return _json({"detail": "only forward experiments are evaluated later"}, 400)
    if exp["status"] == "running":
        return _json({"detail": "experiment is still running"}, 409)
    if exp["status"] == "waiting":
        experiments.evaluate_forward(exp_id, _db_path())
    return _json(experiments.public_detail(_get_experiment_or_404(exp_id), _db_path()))


@app.delete("/api/experiments/{exp_id}")
def experiment_delete(exp_id: int):
    exp = _get_experiment_or_404(exp_id)
    if exp["status"] == "running":
        return _json({"detail": "experiment is running; wait for it to finish"}, 409)
    db.delete_experiment(exp_id, _db_path())
    return _json({"deleted": exp_id})


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

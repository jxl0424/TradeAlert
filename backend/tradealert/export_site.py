"""Static export for GitHub Pages: copy the frontend and snapshot every GET API response as JSON.

    python run.py export-site --out _site

The JSON files are produced by calling the real FastAPI handlers (through a TestClient),
so the shapes can never drift from the live API. The lifespan hooks are NOT run, so no
network access or background threads are started.

Layout under <out>/ (all paths are relative so the site works from a sub-path):
    index.html, app.js, styles.css, ...   copy of frontend/
    site-config.js                        window.TRADEALERT_STATIC = {generated_at, repo}
    .nojekyll
    api/health.json  api/scans.json  api/scans/status.json  api/performance.json
    api/candidates/latest.json  api/candidates/<scan_id>.json
    api/candidates/<scan_id>/<safe ticker>.json   (most recent MAX_SCANS scans only)
    api/experiments.json  api/experiments/defaults.json  api/experiments/<id>.json

`safe_name` must stay in sync with `safeName` in frontend/app.js.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

MAX_SCANS = 30
_KEEP = re.compile(r"[A-Z0-9.\-]")


def safe_name(ticker: str) -> str:
    """Injective, filesystem- and URL-safe file stem for a ticker.

    Upper-cases, keeps A-Z 0-9 . - and writes every other UTF-8 byte as _xx (hex), so '_' itself
    is escaped too. A leading '.' is escaped so the name can never be '.', '..' or a dotfile.
    """
    out = []
    for i, ch in enumerate(str(ticker).upper()):
        if _KEEP.fullmatch(ch) and not (i == 0 and ch == "."):
            out.append(ch)
        else:
            out.extend(f"_{b:02x}" for b in ch.encode("utf-8"))
    return "".join(out) or "_"


class ExportError(RuntimeError):
    pass


def _write(out: Path, rel: str, text: str) -> None:
    target = out / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def export_site(out_dir: str | Path, db_path: str | Path | None = None, repo: str | None = None,
                max_scans: int = MAX_SCANS) -> dict:
    from fastapi.testclient import TestClient

    from . import api

    out = Path(out_dir).resolve()
    if db_path is not None:
        config.DB_PATH = Path(db_path)
    dbp = config.DB_PATH
    db.init_db(dbp)

    frontend = config.FRONTEND_DIR
    if (frontend / "dist").is_dir():
        frontend = frontend / "dist"
    if not frontend.is_dir():
        raise ExportError(f"frontend directory not found: {frontend}")
    out.mkdir(parents=True, exist_ok=True)
    if out == frontend.resolve() or frontend.resolve() in out.parents or out in frontend.resolve().parents:
        raise ExportError("--out must not be inside or contain the frontend directory")
    shutil.rmtree(out / "api", ignore_errors=True)
    shutil.copytree(frontend, out, dirs_exist_ok=True)

    client = TestClient(api.app)  # no `with`: lifespan (network, threads) is deliberately not started
    count = {"files": 0}

    def snap(url: str, rel: str) -> dict:
        r = client.get(url)
        if r.status_code != 200:
            raise ExportError(f"GET {url} returned {r.status_code}: {r.text[:200]}")
        json.loads(r.text)  # must be valid JSON
        _write(out, rel, r.text)
        count["files"] += 1
        return r.json()

    snap("/api/health", "api/health.json")
    scans = snap("/api/scans", "api/scans.json")
    snap("/api/scans/status", "api/scans/status.json")
    snap("/api/performance", "api/performance.json")
    snap("/api/candidates", "api/candidates/latest.json")

    kept = [s for s in scans if s.get("status") == "completed"][:max_scans]
    n_candidates = 0
    for s in kept:
        sid = s["id"]
        data = snap(f"/api/candidates?scan_id={sid}", f"api/candidates/{sid}.json")
        for c in data.get("candidates", []):
            t = c["ticker"]
            try:
                snap(f"/api/candidates/{_quote(t)}?scan_id={sid}", f"api/candidates/{sid}/{safe_name(t)}.json")
                n_candidates += 1
            except ExportError as exc:  # e.g. a ticker containing '/' cannot be routed; skip it, keep the site
                print(f"warning: skipped {t} in scan {sid}: {exc}")

    snap("/api/experiments/defaults", "api/experiments/defaults.json")
    exps = snap("/api/experiments", "api/experiments.json")
    for e in exps:
        snap(f"/api/experiments/{e['id']}", f"api/experiments/{e['id']}.json")

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cfg = {"generated_at": generated_at,
           "repo": (repo if repo is not None else os.environ.get("GITHUB_REPOSITORY", "")).strip(),
           "scan_ids": [s["id"] for s in kept]}  # scans that have candidate files (extra to the documented keys)
    _write(out, "site-config.js", "window.TRADEALERT_STATIC = " + json.dumps(cfg) + ";\n")
    _write(out, ".nojekyll", "")
    return {"out": str(out), "scans": len(kept), "candidates": n_candidates,
            "experiments": len(exps), "json_files": count["files"], **cfg}


def _quote(ticker: str) -> str:
    from urllib.parse import quote

    return quote(ticker, safe="")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run.py export-site", description="Export a static, view-only site.")
    ap.add_argument("--out", required=True, help="output directory (created if missing)")
    ap.add_argument("--db", default=None, help="database path (default: data/tradealert.db)")
    ap.add_argument("--max-scans", type=int, default=MAX_SCANS, help="scans whose candidate files are kept")
    args = ap.parse_args(argv)
    try:
        res = export_site(args.out, db_path=args.db, max_scans=args.max_scans)
    except ExportError as exc:
        print(f"export-site failed: {exc}")
        return 1
    print(f"Exported {res['json_files']} JSON files ({res['scans']} scans, {res['candidates']} candidate "
          f"pages, {res['experiments']} experiments) to {res['out']}")
    return 0

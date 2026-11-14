import json
import re
from pathlib import Path

from tradealert import config, db, export_site
from tradealert.export_site import safe_name

from .test_api import _candidate

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_safe_name_is_safe_and_injective():
    names = ["ABC", "brk.b", "A/B", "A_B", "A B", "..", ".X", "ÄB", "A%2FB", "a-b"]
    out = [safe_name(n) for n in names]
    for n, s in zip(names, out):
        assert re.fullmatch(r"[A-Z0-9.\-_a-f]+", s) and not s.startswith("."), (n, s)
    assert safe_name("A/B") != safe_name("A_B") != safe_name("A B")
    assert safe_name("abc") == "ABC"
    assert len(set(out)) == len(out)


def test_export_site(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FRONTEND_DIR", FRONTEND)
    sid = db.create_scan({}, tmp_db)
    db.update_scan(sid, tmp_db, status="completed", scan_date="2026-11-12", finished_at="2026-11-12T22:00:00+00:00",
                   universe_size=10, candidates_count=2)
    db.insert_candidates(sid, "2026-11-12", [_candidate("ABC", 70, "STRONG"), _candidate("BRK.B", 40, "WEAK")], tmp_db)
    failed = db.create_scan({}, tmp_db)
    db.update_scan(failed, tmp_db, status="failed", message="boom")
    out = tmp_path / "site" / "TradeAlert"
    res = export_site.export_site(out, repo="me/TradeAlert")
    assert res["scans"] == 1 and res["candidates"] == 2

    for rel in ["index.html", "app.js", ".nojekyll", "site-config.js", "api/health.json", "api/scans.json",
                "api/scans/status.json", "api/performance.json", "api/experiments.json",
                "api/experiments/defaults.json", "api/candidates/latest.json", f"api/candidates/{sid}.json",
                f"api/candidates/{sid}/ABC.json", f"api/candidates/{sid}/BRK.B.json"]:
        assert (out / rel).is_file(), rel
    assert not (out / f"api/candidates/{failed}.json").exists()
    for f in (out / "api").rglob("*.json"):
        json.loads(f.read_text(encoding="utf-8"))
    cfg = (out / "site-config.js").read_text()
    assert cfg.startswith("window.TRADEALERT_STATIC = ") and '"repo": "me/TradeAlert"' in cfg
    detail = json.loads((out / f"api/candidates/{sid}/ABC.json").read_text())
    assert detail["ticker"] == "ABC" and detail["signals"]["squeeze_ratio"] is None  # inf cleaned


def test_export_keeps_only_recent_scans(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FRONTEND_DIR", FRONTEND)
    ids = []
    for i in range(4):
        sid = db.create_scan({}, tmp_db)
        db.update_scan(sid, tmp_db, status="completed", scan_date=f"2026-11-0{i + 1}", finished_at="x")
        db.insert_candidates(sid, f"2026-11-0{i + 1}", [_candidate("ABC", 50, "WATCH")], tmp_db)
        ids.append(sid)
    out = tmp_path / "s"
    export_site.export_site(out, max_scans=2)
    assert not (out / f"api/candidates/{ids[0]}.json").exists()
    assert (out / f"api/candidates/{ids[-1]}/ABC.json").exists()


def test_frontend_static_mode_has_no_absolute_api_urls():
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    start = js.index("async function staticGet")
    end = js.index("async function api(")
    block = js[start:end]
    # every URL built for static mode is relative ('api/...'), never '/api/...'
    assert not re.search(r"""[`'"]/api/""", block.replace("replace(/^\\/api\\//", ""))
    assert "`api/" in block or "'api/" in block
    assert "site-config.js" in (FRONTEND / "index.html").read_text()

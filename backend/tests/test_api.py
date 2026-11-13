import math

import pytest
from fastapi.testclient import TestClient

from tradealert import api, config, db, scan


def _candidate(ticker, score, verdict, **kw):
    base = {
        "ticker": ticker, "name": f"{ticker} Inc", "exchange": "NASDAQ CM", "sector": None, "close": 1.23,
        "pct_change_1d": 0.12, "pct_change_5d": float("nan"), "rel_volume": 4.5, "dollar_volume": 5e6,
        "market_cap": None, "float_shares": 9e6, "short_pct_float": None, "score": score, "verdict": verdict,
        "top_reasons": ["volume: 4.5x"], "risk_flags": [{"code": "SUB_DOLLAR", "severity": "low", "text": "x"}],
        "components": [{"name": "volume", "points": 10.0, "max_points": 25, "reason": "r"}],
        "signals": {"rel_volume": 4.5, "squeeze_ratio": float("inf")},
        "bars": [{"time": "2026-11-12", "open": 1, "high": 1.3, "low": 0.9, "close": 1.23, "volume": 100}],
        "news": [{"headline": "h", "source": "s", "url": "u", "published_at": "2026-11-12T10:00:00+00:00"}],
        "filings": [{"form": "8-K", "filed_at": "2026-11-12", "description": "d", "url": "u", "category": "catalyst"}],
        "ai_summary": None,
    }
    base.update(kw)
    return base


@pytest.fixture
def client(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FRONTEND_DIR", tmp_path / "no-frontend")
    s1 = db.create_scan({}, tmp_db)
    db.update_scan(s1, tmp_db, status="completed", scan_date="2026-11-11", finished_at="2026-11-11T22:00:00+00:00",
                   universe_size=100, candidates_count=1)
    db.insert_candidates(s1, "2026-11-11", [_candidate("ABC", 50, "WATCH")], tmp_db)
    s2 = db.create_scan({}, tmp_db)
    db.update_scan(s2, tmp_db, status="completed", scan_date="2026-11-12", finished_at="2026-11-12T22:00:00+00:00",
                   universe_size=120, candidates_count=3)
    db.insert_candidates(s2, "2026-11-12", [
        _candidate("ABC", 70, "STRONG"), _candidate("XYZ", 30, "WEAK"), _candidate("DIL", 60, "AVOID"),
    ], tmp_db)
    with TestClient(api.app) as c:
        c.scan_ids = (s1, s2)
        yield c


def test_health(client):
    r = client.get("/api/health").json()
    assert r == {"status": "ok", "last_scan_at": "2026-11-12T22:00:00+00:00", "ai_enabled": False}


def test_scans_newest_first(client):
    r = client.get("/api/scans").json()
    assert [s["id"] for s in r] == [client.scan_ids[1], client.scan_ids[0]]
    assert set(r[0]) == {"id", "scan_date", "started_at", "finished_at", "status", "universe_size", "candidates_count"}


def test_candidates_list_sorted_filtered_and_nan_is_null(client):
    r = client.get("/api/candidates").json()
    assert r["scan"]["id"] == client.scan_ids[1]
    assert [c["ticker"] for c in r["candidates"]] == ["ABC", "DIL", "XYZ"]
    c = r["candidates"][0]
    assert c["pct_change_5d"] is None
    assert set(c) == {"ticker", "name", "exchange", "sector", "close", "pct_change_1d", "pct_change_5d",
                      "rel_volume", "dollar_volume", "market_cap", "float_shares", "short_pct_float", "score",
                      "verdict", "top_reasons", "risk_flags"}
    assert c["risk_flags"][0]["severity"] == "low"
    assert [x["ticker"] for x in client.get("/api/candidates?verdict=avoid").json()["candidates"]] == ["DIL"]
    assert [x["ticker"] for x in client.get("/api/candidates?min_score=55").json()["candidates"]] == ["ABC", "DIL"]
    old = client.get(f"/api/candidates?scan_id={client.scan_ids[0]}").json()
    assert [x["ticker"] for x in old["candidates"]] == ["ABC"]
    assert client.get("/api/candidates?scan_id=999").status_code == 404


def test_candidate_detail(client):
    r = client.get("/api/candidates/abc")
    assert r.status_code == 200
    c = r.json()
    for key in ("components", "signals", "bars", "news", "filings", "ai_summary", "history", "risk_flags",
                "top_reasons", "score", "verdict"):
        assert key in c
    assert c["signals"]["squeeze_ratio"] is None  # inf -> null
    assert c["components"][0]["max_points"] == 25
    assert c["bars"][0]["time"] == "2026-11-12"
    assert c["history"] == [{"scan_date": "2026-11-11", "score": 50.0, "verdict": "WATCH"}]
    assert client.get("/api/candidates/NOPE").status_code == 404


def test_performance_shape(client):
    r = client.get("/api/performance").json()
    assert r["hit_threshold_pct"] == config.HIT_THRESHOLD_PCT
    assert [v["verdict"] for v in r["by_verdict"]] == ["STRONG", "WATCH", "WEAK", "AVOID"]
    assert {"verdict", "count", "avg_close_return_5d", "median_close_return_5d", "avg_max_gain_5d", "hit_rate",
            "avg_max_drawdown_5d"} <= set(r["by_verdict"][0])
    assert len(r["picks"]) == 4
    p = r["picks"][0]
    assert set(p) == {"scan_date", "ticker", "verdict", "score", "entry_close", "ret_1d", "ret_3d", "ret_5d",
                      "ret_10d", "max_gain_5d", "max_drawdown_5d"}
    assert p["ret_5d"] is None


def test_status_and_conflict(client, monkeypatch):
    st = client.get("/api/scans/status").json()
    assert set(st) == {"running", "scan_id", "stage", "progress", "message"}
    monkeypatch.setattr(scan.STATE, "running", True)
    r = client.post("/api/scans/run")
    assert r.status_code == 409
    monkeypatch.setattr(scan.STATE, "running", False)


def test_run_starts_background_scan(client, monkeypatch):
    def fake_run_scan(state, **kwargs):
        sid = db.create_scan({}, kwargs.get("db_path"))
        state.update(stage="prices", progress=0.1, message="x", scan_id=sid)
        state.finish("completed", "done")
        return sid

    monkeypatch.setattr(scan, "run_scan", fake_run_scan)
    r = client.post("/api/scans/run")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running" and isinstance(body["scan_id"], int)


def test_frontend_missing_is_tolerated(client):
    r = client.get("/")
    assert r.status_code == 200 and "frontend" in r.json()["detail"]
    assert client.get("/api/nope").status_code == 404


def test_frontend_served_with_index_fallback(client, tmp_path, monkeypatch):
    fe = tmp_path / "fe"
    (fe / "assets").mkdir(parents=True)
    (fe / "index.html").write_text("<html>TradeAlert</html>", encoding="utf-8")
    (fe / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(config, "FRONTEND_DIR", fe)
    assert "TradeAlert" in client.get("/").text
    assert "TradeAlert" in client.get("/candidates/ABC").text  # SPA route -> index.html
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/../../etc/passwd").status_code in (200, 404)


def test_empty_db(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FRONTEND_DIR", tmp_path / "none")
    with TestClient(api.app) as c:
        assert c.get("/api/candidates").json() == {"scan": None, "candidates": []}
        assert c.get("/api/health").json()["last_scan_at"] is None
        assert c.get("/api/scans").json() == []
        perf = c.get("/api/performance").json()
        assert perf["picks"] == [] and all(v["count"] == 0 for v in perf["by_verdict"])

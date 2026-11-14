"""Convenience launcher (works from the project root on Windows and Linux).

    python run.py scan [--limit N] [--ai-top N]   # run one scan
    python run.py serve [--port 8000]             # start the dashboard/API
    python run.py experiment backtest [--start YYYY-MM-DD --end YYYY-MM-DD --horizon 5 --top-n 10 ...]
    python run.py experiment forward [--scan-id N | --fresh-scan] [--verdicts STRONG,WATCH ...]
    python run.py experiment evaluate [--id N]    # settle waiting forward experiments now
    python run.py experiment list | show N
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND))


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("scan", "serve", "experiment"):
        print(__doc__)
        return 2
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "experiment":
        from tradealert.experiments import main as experiment_main

        return experiment_main(rest)
    if cmd == "scan":
        from tradealert.scan import main as scan_main

        return scan_main(rest)
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(rest)
    uvicorn.run("tradealert.api:app", host=args.host, port=args.port, app_dir=str(BACKEND))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

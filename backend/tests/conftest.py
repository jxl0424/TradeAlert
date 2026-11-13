import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_bars(closes, volumes=None, start="2026-01-02", spread=0.02, opens=None, highs=None, lows=None):
    """Synthetic business-day OHLCV bars from a list of closes."""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.bdate_range(start, periods=n)
    volumes = np.full(n, 1_000_000.0) if volumes is None else np.asarray(volumes, dtype=float)
    o = np.r_[closes[0], closes[:-1]] if opens is None else np.asarray(opens, dtype=float)
    h = np.maximum(o, closes) * (1 + spread) if highs is None else np.asarray(highs, dtype=float)
    l = np.minimum(o, closes) * (1 - spread) if lows is None else np.asarray(lows, dtype=float)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": closes, "Volume": volumes}, index=idx)


@pytest.fixture
def bars_factory():
    return make_bars


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    from tradealert import config, db

    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    db.init_db(path)
    return path

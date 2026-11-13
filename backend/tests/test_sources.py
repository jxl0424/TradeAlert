"""Parsers for universe files, yfinance news, bars normalization, and the AI wrapper (no network)."""
import datetime as dt
from types import SimpleNamespace

import pandas as pd

from tradealert import ai_summary, config, fundamentals, news, prices, universe

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
AAAP|Pacer Barings CLO ETF|G|N|N|100|Y|N
ZZTST|Test Issue Common Stock|S|Y|N|100|N|N
ABCDW|Abc Acquisition Corp. - Warrant|S|N|N|100|N|N
ABCDU|Abc Acquisition Corp. - Units|S|N|N|100|N|N
ABCDR|Abc Acquisition Corp. - Rights|S|N|N|100|N|N
PENY|Penny Corp - Common Stock|S|N|D|100|N|N
PFDX|Foo Bank - 7.5% Series A Preferred Stock|G|N|N|100|N|N
File Creation Time: 1113202612:11|||||||
"""
OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. Class B Common Stock|N|BRK.B|N|100|N|BRK.B
ABR$D|Arbor Realty Trust 6.375% Series D Preferred Stock|N|ABR$D|N|100|N|ABR$D
AAC.U|Ares Acquisition Corporation III Units, each consisting of one share|N|AAC.U|N|100|N|AAC.U
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
EPD|Enterprise Products Partners L.P. Common Units|N|EPD|N|100|N|EPD
File Creation Time: 1113202612:11||||||
"""


def test_parse_universe_files():
    nl = {s.ticker: s for s in universe.parse_nasdaq_listed(NASDAQ)}
    assert set(nl) == {"AAPL", "PENY"}
    assert nl["PENY"].financial_status == "D" and nl["AAPL"].financial_status is None
    assert nl["PENY"].exchange == "NASDAQ CM"
    ol = {s.ticker: s for s in universe.parse_other_listed(OTHER)}
    assert set(ol) == {"BRK-B", "EPD"}
    assert ol["BRK-B"].symbol == "BRK.B" and ol["BRK-B"].exchange == "NYSE"


def test_parse_yf_news_new_and_legacy_shapes():
    items = [
        {"content": {"title": "Co wins contract", "pubDate": "2026-11-12T13:00:00Z",
                     "provider": {"displayName": "GlobeNewswire"},
                     "canonicalUrl": {"url": "https://x/1"}, "summary": "s"}},
        {"title": "Legacy headline", "publisher": "Reuters", "link": "https://x/2", "providerPublishTime": 1762952400},
        {"content": {"title": None}},
    ]
    out = news.parse_yf_news(items)
    assert [n["headline"] for n in out] == ["Co wins contract", "Legacy headline"]
    assert out[0]["source"] == "GlobeNewswire" and out[0]["url"] == "https://x/1"
    assert out[0]["published_at"].startswith("2026-11-12T13:00:00")
    assert out[1]["source"] == "Reuters" and out[1]["published_at"] is not None


def test_recent_news_count():
    asof = dt.date(2026, 11, 13)
    items = [{"published_at": "2026-11-12T10:00:00+00:00"}, {"published_at": "2026-11-01T10:00:00+00:00"},
             {"published_at": None}]
    assert news.recent_news_count(items, asof, 3) == 1


def test_split_download_and_incomplete_bar(monkeypatch):
    idx = pd.to_datetime(["2026-11-11", "2026-11-12"])
    cols = pd.MultiIndex.from_product([["AAA", "BBB"], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    data = pd.DataFrame([[1, 1.1, 0.9, 1.0, 1.0, 100, None, None, None, None, None, None],
                         [1, 1.2, 0.9, 1.1, 1.1, 200, None, None, None, None, None, None]], index=idx, columns=cols)
    out = prices.split_download(data, ["AAA", "BBB"])
    assert list(out) == ["AAA"]  # BBB had no data
    assert list(out["AAA"].columns) == ["Open", "High", "Low", "Close", "Volume"]
    # pretend it's 11:00 ET on 2026-11-12: today's bar is incomplete and gets dropped
    monkeypatch.setattr(prices, "is_incomplete_bar_date", lambda d: d == dt.date(2026, 11, 12))
    out2 = prices.split_download(data, ["AAA"])
    assert out2["AAA"].index[-1] == pd.Timestamp("2026-11-11")


def test_filter_tradeable(bars_factory):
    asof = None
    bars = {
        "OK": bars_factory([1.0] * 30, [500_000] * 30),
        "PRICEY": bars_factory([10.0] * 30, [500_000] * 30),
        "ILLIQ": bars_factory([1.0] * 30, [10_000] * 30),
        "SHORT": bars_factory([1.0] * 5, [500_000] * 5),
    }
    asof = bars["OK"].index[-1]
    assert set(prices.filter_tradeable(bars, asof=asof)) == {"OK"}


def test_parse_info_and_reverse_split():
    info = fundamentals.parse_info({"floatShares": 5e6, "shortPercentOfFloat": 12.0, "sector": "Tech"})
    assert info["short_pct_float"] == 0.12 and info["market_cap"] is None
    splits = pd.Series([0.1], index=pd.to_datetime(["2026-11-01"]))
    rs = fundamentals.recent_reverse_split(splits, dt.date(2026, 11, 13))
    assert rs["ratio"] == 0.1 and rs["days_ago"] == 12
    assert fundamentals.recent_reverse_split(pd.Series([2.0], index=pd.to_datetime(["2026-11-01"])),
                                             dt.date(2026, 11, 13)) is None


class _FakeMessages:
    def __init__(self, text=None, exc=None, stop_reason="end_turn"):
        self.text, self.exc, self.stop_reason, self.calls = text, exc, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(stop_reason=self.stop_reason, model=kwargs["model"],
                               content=[SimpleNamespace(type="text", text=self.text)])


def _cand():
    return {"ticker": "ABC", "name": "ABC Inc", "signals": {"close": 1.0}, "components": [], "risk_flags": [],
            "filings": [], "news": [], "score": 70}


def test_ai_summary_parses_structured_output(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    msgs = _FakeMessages(text='{"bull_case":"b","bear_case":"r","catalyst_read":"c","key_risk":"k"}')
    out = ai_summary.summarize_candidate(_cand(), client=SimpleNamespace(messages=msgs), model="claude-haiku-5-5")
    assert out["bull_case"] == "b" and out["key_risk"] == "k" and out["model"] == "claude-haiku-5-5"
    call = msgs.calls[0]
    assert call["max_tokens"] == config.AI_MAX_TOKENS
    assert call["output_config"]["format"]["type"] == "json_schema"


def test_ai_summary_never_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    boom = SimpleNamespace(messages=_FakeMessages(exc=RuntimeError("boom")))
    assert ai_summary.summarize_candidate(_cand(), client=boom) is None
    refusal = SimpleNamespace(messages=_FakeMessages(text="", stop_reason="refusal"))
    assert ai_summary.summarize_candidate(_cand(), client=refusal) is None
    junk = SimpleNamespace(messages=_FakeMessages(text="not json"))
    assert ai_summary.summarize_candidate(_cand(), client=junk) is None


def test_ai_disabled_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert config.ai_enabled() is False
    assert ai_summary.summarize_candidate(_cand()) is None

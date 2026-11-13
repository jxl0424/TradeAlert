"""Optional AI explanation of a candidate (Claude via the official `anthropic` SDK).

The AI only explains the evidence; it never changes the score or verdict.
Enabled when ANTHROPIC_API_KEY is set. Any error returns None (the scan continues).
"""
from __future__ import annotations

import json
import os

from . import config
from .utils import log, utc_now_iso

SYSTEM_PROMPT = """You are an analyst assistant inside a personal penny-stock screening tool.
You receive the screener's evidence for ONE US-listed stock: price/volume signals, \
score components, risk flags, recent SEC filings (form types, 8-K item numbers, \
descriptions) and recent news headlines with short summaries.

Write a short, evidence-based read for a human who will make their own decision.
Rules:
- Use only the evidence provided. Do not invent news, numbers, or filing contents. \
You see filing form types and item numbers, not the documents; say so when it matters.
- If the evidence is thin, say that plainly rather than speculating.
- Treat dilution filings (S-1, S-3, 424B*, EFFECT, 8-K item 3.02), late filings, \
delisting notices and reverse splits as serious risks for penny stocks.
- Do not give buy/sell instructions or price targets. Do not restate the score.
- Each field: 1-3 sentences, plain English."""

SCHEMA = {
    "type": "object",
    "properties": {
        "bull_case": {"type": "string", "description": "What in the evidence supports a further move up."},
        "bear_case": {"type": "string", "description": "What in the evidence argues against it."},
        "catalyst_read": {"type": "string",
                          "description": "What the news and filings actually say (or that there is none)."},
        "key_risk": {"type": "string", "description": "The single most important risk."},
    },
    "required": ["bull_case", "bear_case", "catalyst_read", "key_risk"],
    "additionalProperties": False,
}

_SIGNAL_KEYS = ("close", "pct_change_1d", "pct_change_5d", "pct_change_20d", "rel_volume", "vol_trend_3d",
                "dollar_volume", "avg_dollar_volume_20d", "breakout_20d", "gap_pct", "close_position",
                "range_expansion", "squeeze_ratio", "rsi14", "pct_from_period_high", "pct_from_period_low")


def build_evidence(c: dict) -> dict:
    sig = c.get("signals") or {}
    return {
        "ticker": c.get("ticker"),
        "name": c.get("name"),
        "exchange": c.get("exchange"),
        "sector": c.get("sector"),
        "as_of": c.get("scan_date"),
        "fundamentals": {k: c.get(k) for k in ("market_cap", "float_shares", "short_pct_float")},
        "signals": {k: sig.get(k) for k in _SIGNAL_KEYS},
        "score_components": [
            {k: comp.get(k) for k in ("name", "points", "max_points", "reason")}
            for comp in c.get("components") or []
        ],
        "risk_flags": c.get("risk_flags") or [],
        "sec_filings_last_30d": [
            {k: f.get(k) for k in ("form", "filed_at", "items", "description", "category")}
            for f in (c.get("filings") or [])[:15]
        ],
        "news": [
            {k: n.get(k) for k in ("headline", "source", "published_at", "summary")}
            for n in (c.get("news") or [])[:10]
        ],
    }


def _extract_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def summarize_candidate(c: dict, client=None, model: str | None = None) -> dict | None:
    """One Claude call for one candidate. Returns the ai_summary dict or None."""
    if not config.ai_enabled() and client is None:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed; skipping AI summary")
        return None
    model = model or os.environ.get("TRADEALERT_AI_MODEL", config.AI_MODEL)
    try:
        client = client or anthropic.Anthropic(timeout=60.0, max_retries=2)
        evidence = json.dumps(build_evidence(c), default=str)
        response = client.messages.create(
            model=model,
            max_tokens=config.AI_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Evidence (JSON):\n{evidence}"}],
            output_config={
                "effort": config.AI_EFFORT,
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
        )
    except anthropic.APIStatusError as exc:
        log.warning("AI summary for %s failed: HTTP %s %s", c.get("ticker"), exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("AI summary for %s failed: connection error %s", c.get("ticker"), exc)
        return None
    except Exception as exc:  # noqa: BLE001 - never fail the scan on AI errors
        log.warning("AI summary for %s failed: %s", c.get("ticker"), exc)
        return None

    if response.stop_reason in ("refusal", "max_tokens"):
        log.warning("AI summary for %s stopped: %s", c.get("ticker"), response.stop_reason)
        return None
    text = "".join(b.text for b in response.content if b.type == "text")
    data = _extract_json(text)
    if not data:
        log.warning("AI summary for %s: unparseable response", c.get("ticker"))
        return None
    return {
        "bull_case": str(data.get("bull_case", "")).strip() or None,
        "bear_case": str(data.get("bear_case", "")).strip() or None,
        "catalyst_read": str(data.get("catalyst_read", "")).strip() or None,
        "key_risk": str(data.get("key_risk", "")).strip() or None,
        "model": getattr(response, "model", None) or model,
        "generated_at": utc_now_iso(),
    }

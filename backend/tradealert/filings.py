"""SEC EDGAR filings: ticker->CIK map, recent submissions, catalyst vs dilution/risk classification.

SEC requires a descriptive User-Agent (env SEC_USER_AGENT) and allows <=10 req/s.
"""
from __future__ import annotations

import datetime as dt
import json
import threading

from . import config
from .utils import RateLimiter, http_session, log, now_et

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

_limiter = RateLimiter(config.SEC_MAX_REQ_PER_SEC)
_session_lock = threading.Lock()
_session = None

# --------------------------------------------------------------------------- classification tables
# 8-K item numbers -> (category, label). Category: catalyst | dilution | risk | routine.
ITEMS_8K = {
    "1.01": ("catalyst", "Entry into a material definitive agreement"),
    "1.02": ("risk", "Termination of a material definitive agreement"),
    "1.03": ("risk", "Bankruptcy or receivership"),
    "1.05": ("risk", "Material cybersecurity incident"),
    "2.01": ("catalyst", "Completion of acquisition or disposition of assets"),
    "2.02": ("catalyst", "Results of operations and financial condition"),
    "2.03": ("risk", "Creation of a direct financial obligation"),
    "2.04": ("risk", "Triggering events accelerating a financial obligation"),
    "2.05": ("risk", "Costs associated with exit or disposal activities"),
    "2.06": ("risk", "Material impairments"),
    "3.01": ("risk", "Notice of delisting or failure to satisfy a listing rule"),
    "3.02": ("dilution", "Unregistered sales of equity securities"),
    "3.03": ("risk", "Material modification to rights of security holders"),
    "4.01": ("risk", "Change in registrant's certifying accountant"),
    "4.02": ("risk", "Non-reliance on previously issued financial statements"),
    "5.01": ("catalyst", "Change in control of registrant"),
    "5.02": ("routine", "Departure/appointment of directors or officers"),
    "5.03": ("risk", "Amendments to articles/bylaws (often a reverse split)"),
    "5.07": ("routine", "Submission of matters to a vote of security holders"),
    "7.01": ("catalyst", "Regulation FD disclosure"),
    "8.01": ("catalyst", "Other events"),
    "9.01": ("routine", "Financial statements and exhibits"),
}
# 8-K items that justify a risk flag: item -> (flag code, severity)
RISK_ITEMS_8K = {
    "1.03": ("BANKRUPTCY", "high"),
    "3.01": ("DELISTING_NOTICE", "high"),
    "3.02": ("DILUTION_UNREGISTERED_SALE", "high"),
    "4.02": ("NON_RELIANCE", "high"),
    "2.04": ("DEBT_ACCELERATION", "medium"),
    "3.03": ("HOLDER_RIGHTS_CHANGE", "medium"),
    "5.03": ("POSSIBLE_REVERSE_SPLIT", "medium"),
    "4.01": ("AUDITOR_CHANGE", "medium"),
}
CATALYST_ITEMS = {k for k, (cat, _) in ITEMS_8K.items() if cat == "catalyst"}

# form -> (category, flag code or None, severity, description)
DILUTION_FORMS = {
    "S-1": ("DILUTION_S1", "high", "Registration statement (S-1): new share offering or resale"),
    "S-1/A": ("DILUTION_S1", "high", "Amended S-1 registration (offering in progress)"),
    "F-1": ("DILUTION_S1", "high", "Foreign issuer registration (F-1): share offering"),
    "F-1/A": ("DILUTION_S1", "high", "Amended F-1 registration"),
    "S-1MEF": ("DILUTION_S1", "high", "S-1 upsizing (MEF)"),
    "S-3": ("DILUTION_SHELF", "medium", "Shelf registration (S-3): can sell shares / ATM"),
    "S-3/A": ("DILUTION_SHELF", "medium", "Amended S-3 shelf registration"),
    "S-3ASR": ("DILUTION_SHELF", "medium", "Automatic shelf registration"),
    "F-3": ("DILUTION_SHELF", "medium", "Foreign issuer shelf registration (F-3)"),
    "F-3/A": ("DILUTION_SHELF", "medium", "Amended F-3 shelf registration"),
    "424B1": ("DILUTION_PROSPECTUS", "high", "Final prospectus: offering priced"),
    "424B2": ("DILUTION_PROSPECTUS", "high", "Prospectus supplement: securities offering"),
    "424B3": ("DILUTION_PROSPECTUS", "high", "Prospectus (424B3): offering/resale of shares"),
    "424B4": ("DILUTION_PROSPECTUS", "high", "Final prospectus: offering priced"),
    "424B5": ("DILUTION_PROSPECTUS", "high", "Prospectus supplement: shelf takedown / ATM"),
    "424B7": ("DILUTION_PROSPECTUS", "high", "Prospectus supplement: selling holders"),
    "424B8": ("DILUTION_PROSPECTUS", "high", "Prospectus (late filed)"),
    "EFFECT": ("DILUTION_EFFECTIVE", "medium", "Registration declared effective: shares can now be sold"),
    "POS AM": ("DILUTION_SHELF", "low", "Post-effective amendment to a registration"),
    "S-8": ("DILUTION_S8", "low", "Employee stock plan registration (S-8)"),
}
RISK_FORMS = {
    "NT 10-K": ("LATE_FILING", "high", "Notification of late annual report (NT 10-K)"),
    "NT 10-Q": ("LATE_FILING", "medium", "Notification of late quarterly report (NT 10-Q)"),
    "NT 20-F": ("LATE_FILING", "high", "Notification of late annual report (NT 20-F)"),
    "25-NSE": ("DELISTING", "high", "Notice of removal from listing (Form 25)"),
    "25": ("DELISTING", "high", "Notice of removal from listing (Form 25)"),
    "15-12B": ("DEREGISTRATION", "high", "Termination of registration (Form 15)"),
    "15-12G": ("DEREGISTRATION", "high", "Termination of registration (Form 15)"),
    "PRE 14A": (None, None, "Preliminary proxy (check for reverse split / share increase)"),
    "PRE 14C": (None, None, "Preliminary information statement (check for reverse split)"),
}
CATALYST_FORMS = {
    "SC 13D": "Activist/large stake disclosed (13D)",
    "SCHEDULE 13D": "Activist/large stake disclosed (13D)",
    "SC 13D/A": "Amended 13D stake disclosure",
    "SCHEDULE 13D/A": "Amended 13D stake disclosure",
}
ROUTINE_DESCRIPTIONS = {
    "10-K": "Annual report", "10-Q": "Quarterly report", "20-F": "Foreign annual report",
    "6-K": "Foreign issuer report (6-K)", "4": "Insider transaction (Form 4)", "3": "Initial insider holding",
    "DEF 14A": "Definitive proxy statement", "SC 13G": "Passive stake disclosure (13G)",
    "SCHEDULE 13G": "Passive stake disclosure (13G)", "144": "Proposed insider sale (Form 144)",
}


def parse_items(items: str | None) -> list[str]:
    if not items:
        return []
    return [x.strip() for x in str(items).replace(";", ",").split(",") if x.strip()]


def classify_filing(form: str, items: str | None = None, description: str | None = None) -> dict:
    """Classify one filing.

    Returns {category, description, flags: [{code, severity, text}], catalyst_items: [...]}.
    """
    form = (form or "").strip().upper()
    desc_in = (description or "").strip()
    flags: list[dict] = []
    if form in ("8-K", "8-K/A"):
        its = parse_items(items)
        labels, cats = [], set()
        for it in its:
            cat, label = ITEMS_8K.get(it, ("routine", f"Item {it}"))
            labels.append(f"{it} {label}")
            cats.add(cat)
            if it in RISK_ITEMS_8K:
                code, sev = RISK_ITEMS_8K[it]
                flags.append({"code": code, "severity": sev, "text": f"8-K item {it}: {label}"})
        if "reverse" in desc_in.lower() and "split" in desc_in.lower():
            flags.append({"code": "REVERSE_SPLIT", "severity": "high", "text": f"8-K mentions reverse split: {desc_in}"})
            cats.add("risk")
        # priority: dilution > risk > catalyst > routine
        category = next((c for c in ("dilution", "risk", "catalyst") if c in cats), "routine")
        desc = "8-K: " + ("; ".join(labels) if labels else (desc_in or "current report"))
        return {"category": category, "description": desc, "flags": flags,
                "catalyst_items": [i for i in its if i in CATALYST_ITEMS]}
    if form in DILUTION_FORMS:
        code, sev, text = DILUTION_FORMS[form]
        return {"category": "dilution", "description": text,
                "flags": [{"code": code, "severity": sev, "text": f"{form} filed: {text}"}], "catalyst_items": []}
    if form in RISK_FORMS:
        code, sev, text = RISK_FORMS[form]
        if code:
            flags.append({"code": code, "severity": sev, "text": f"{form} filed: {text}"})
        return {"category": "risk", "description": text, "flags": flags, "catalyst_items": []}
    if form in CATALYST_FORMS:
        return {"category": "catalyst", "description": CATALYST_FORMS[form], "flags": [], "catalyst_items": []}
    if form.startswith("424B"):
        return {"category": "dilution", "description": "Prospectus supplement",
                "flags": [{"code": "DILUTION_PROSPECTUS", "severity": "high", "text": f"{form} filed: prospectus"}],
                "catalyst_items": []}
    return {"category": "routine", "description": ROUTINE_DESCRIPTIONS.get(form, desc_in or form),
            "flags": [], "catalyst_items": []}


def summarize_filings(filings: list[dict], asof: dt.date, fresh_days: int = 10) -> dict:
    """Aggregate classified filings into deduped risk flags and catalyst evidence.

    Dilution/risk flags older than `fresh_days` are downgraded one severity level
    (a 25-day-old S-3 is less urgent than yesterday's 424B5).
    """
    downgrade = {"high": "medium", "medium": "low", "low": "low"}
    best: dict[str, dict] = {}
    catalysts: list[dict] = []
    for f in filings:
        try:
            age = (asof - dt.date.fromisoformat(f["filed_at"][:10])).days
        except (KeyError, ValueError, TypeError):
            age = 999
        for flag in f.get("flags", []):
            sev = flag["severity"] if age <= fresh_days else downgrade[flag["severity"]]
            text = f"{flag['text']} ({f['filed_at'][:10]}, {age}d ago)"
            cur = best.get(flag["code"])
            rank = {"high": 3, "medium": 2, "low": 1}
            if cur is None or rank[sev] > rank[cur["severity"]]:
                best[flag["code"]] = {"code": flag["code"], "severity": sev, "text": text}
        if f.get("category") == "catalyst":
            catalysts.append({**f, "age_days": age})
    return {"risk_flags": list(best.values()), "catalysts": catalysts}


# --------------------------------------------------------------------------- network
def _sec_session():
    global _session
    with _session_lock:
        if _session is None:
            ua = config.sec_user_agent()
            if not ua:
                raise RuntimeError("SEC_USER_AGENT is not set (e.g. 'TradeAlert research tool you@example.com')")
            _session = http_session(ua)
        return _session


def _sec_get_json(url: str) -> dict | list | None:
    _limiter.wait()
    r = _sec_session().get(url, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def load_ticker_map(force: bool = False) -> dict[str, dict]:
    """{TICKER: {cik, title}} cached once per day."""
    config.ensure_dirs()
    cache = config.CACHE_DIR / f"sec_tickers_{now_et().date().isoformat()}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    data = _sec_get_json(TICKER_MAP_URL) or {}
    rows = data.values() if isinstance(data, dict) else data
    out = {}
    for row in rows:
        t = str(row.get("ticker", "")).upper().replace(".", "-")
        if t and t not in out:
            out[t] = {"cik": int(row["cik_str"]), "title": row.get("title")}
    cache.write_text(json.dumps(out), encoding="utf-8")
    for old in config.CACHE_DIR.glob("sec_tickers_*.json"):
        if old != cache:
            old.unlink(missing_ok=True)
    return out


def fetch_recent_filings(cik: int, asof: dt.date, lookback_days: int | None = None) -> list[dict]:
    """Recent filings (newest first) with classification, within the lookback window."""
    lookback = lookback_days or config.FILINGS_LOOKBACK_DAYS
    data = _sec_get_json(SUBMISSIONS_URL.format(cik=cik))
    if not data:
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    cutoff = asof - dt.timedelta(days=lookback)
    out = []
    for i, form in enumerate(forms):
        filed = recent.get("filingDate", [None] * len(forms))[i]
        if not filed:
            continue
        fdate = dt.date.fromisoformat(filed)
        if fdate < cutoff:
            break  # list is newest-first
        if fdate > asof:
            continue  # filed after the scan day: excluded to avoid look-ahead on re-scans
        acc = recent.get("accessionNumber", [""] * len(forms))[i]
        doc = recent.get("primaryDocument", [""] * len(forms))[i]
        items = recent.get("items", [""] * len(forms))[i]
        pdesc = recent.get("primaryDocDescription", [""] * len(forms))[i]
        cls = classify_filing(form, items, pdesc)
        url = (ARCHIVE_URL.format(cik=cik, acc=acc.replace("-", ""), doc=doc) if doc
               else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}")
        out.append({
            "form": form,
            "filed_at": filed,
            "description": cls["description"],
            "url": url,
            "category": cls["category"],
            "items": items or None,
            "flags": cls["flags"],
            "catalyst_items": cls["catalyst_items"],
        })
    return out


def filings_for_ticker(ticker: str, asof: dt.date, ticker_map: dict[str, dict]) -> dict:
    """Return {cik, filings, risk_flags, catalysts, error}. Never raises."""
    info = ticker_map.get(ticker.upper())
    if not info:
        return {"cik": None, "filings": [], "risk_flags": [], "catalysts": [], "error": "no CIK"}
    try:
        filings = fetch_recent_filings(info["cik"], asof)
    except Exception as exc:  # noqa: BLE001
        log.warning("EDGAR fetch failed for %s: %s", ticker, exc)
        return {"cik": info["cik"], "filings": [], "risk_flags": [], "catalysts": [], "error": str(exc)}
    summary = summarize_filings(filings, asof)
    return {"cik": info["cik"], "filings": filings, **summary, "error": None}

import datetime as dt

from tradealert.filings import classify_filing, summarize_filings


def test_8k_catalyst_items():
    r = classify_filing("8-K", "1.01,9.01")
    assert r["category"] == "catalyst"
    assert r["catalyst_items"] == ["1.01"]
    assert "material definitive agreement" in r["description"].lower()
    assert r["flags"] == []


def test_8k_unregistered_sale_is_dilution():
    r = classify_filing("8-K", "3.02,9.01")
    assert r["category"] == "dilution"
    assert r["flags"][0]["code"] == "DILUTION_UNREGISTERED_SALE"
    assert r["flags"][0]["severity"] == "high"


def test_8k_delisting_notice_is_risk():
    r = classify_filing("8-K", "3.01")
    assert r["category"] == "risk"
    assert r["flags"][0]["code"] == "DELISTING_NOTICE"


def test_8k_mixed_items_dilution_wins_over_catalyst():
    r = classify_filing("8-K", "1.01,3.02,8.01")
    assert r["category"] == "dilution"
    assert set(r["catalyst_items"]) == {"1.01", "8.01"}


def test_registration_and_prospectus_forms():
    assert classify_filing("S-1")["flags"][0]["severity"] == "high"
    assert classify_filing("S-1/A")["category"] == "dilution"
    s3 = classify_filing("S-3")
    assert s3["category"] == "dilution" and s3["flags"][0]["severity"] == "medium"
    p = classify_filing("424B5")
    assert p["category"] == "dilution" and p["flags"][0]["code"] == "DILUTION_PROSPECTUS"
    assert classify_filing("424B9")["category"] == "dilution"  # unknown 424B variant still dilution
    assert classify_filing("EFFECT")["flags"][0]["code"] == "DILUTION_EFFECTIVE"


def test_late_filing_and_routine():
    nt = classify_filing("NT 10-K")
    assert nt["category"] == "risk" and nt["flags"][0]["code"] == "LATE_FILING"
    assert classify_filing("10-Q")["category"] == "routine"
    assert classify_filing("4")["category"] == "routine"
    assert classify_filing("SC 13D")["category"] == "catalyst"


def test_summary_downgrades_old_flags_and_dedupes():
    asof = dt.date(2026, 11, 13)
    filings = [
        {"form": "424B5", "filed_at": "2026-11-12", "category": "dilution", **{k: v for k, v in classify_filing("424B5").items() if k == "flags"}},
        {"form": "424B5", "filed_at": "2026-10-20", "category": "dilution", **{k: v for k, v in classify_filing("424B5").items() if k == "flags"}},
        {"form": "S-3", "filed_at": "2026-10-20", "category": "dilution", **{k: v for k, v in classify_filing("S-3").items() if k == "flags"}},
        {"form": "8-K", "filed_at": "2026-11-11", "category": "catalyst", "flags": [], "description": "8-K"},
    ]
    s = summarize_filings(filings, asof)
    flags = {f["code"]: f for f in s["risk_flags"]}
    assert flags["DILUTION_PROSPECTUS"]["severity"] == "high"  # the fresh one wins
    assert "1d ago" in flags["DILUTION_PROSPECTUS"]["text"]
    assert flags["DILUTION_SHELF"]["severity"] == "low"  # 24 days old: medium -> low
    assert len(s["catalysts"]) == 1 and s["catalysts"][0]["age_days"] == 2

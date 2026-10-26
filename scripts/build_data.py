#!/usr/bin/env python3
"""
AI Backlog Radar - data builder
Source: SEC EDGAR public APIs (data.sec.gov). No API key; a descriptive
User-Agent with contact email is required (GitHub Secret SEC_USER_AGENT).

Output: data/backlog.json
  - universe : per-company RPO (remaining performance obligation) history,
               quarterly revenue, coverage (RPO / trailing-4Q revenue)
  - aggregate: RPO sum by calendar quarter, total and per category
  - capex    : hyperscaler quarterly capital expenditure
  - screener : whole-market RPO ranking from the XBRL frames API
"""
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "universe.json"
OUT = ROOT / "data" / "backlog.json"

RPO_TAG = "RevenueRemainingPerformanceObligation"
REV_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
]
CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
]
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT"}
KEEP_POINTS = 12          # quarters kept per series in the output
STALE_DAYS = 200          # latest RPO older than this -> tag discontinued
YOY_BASE_MIN = 500_000_000  # screener growth needs a prior value >= $500M
DIAG_KEYS = ("remainingperformanceobligation", "backlog", "unsatisfiedperformance",
             "transactionpriceallocated", "contractedrevenue")
REQ_GAP = 0.15            # seconds between requests (SEC limit: 10 req/s)

UA = os.environ.get("SEC_USER_AGENT", "").strip()


# ---------------------------------------------------------------- http
def get_json(url, allow_404=False):
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "application/json"}
    last_err = None
    for attempt in range(5):
        time.sleep(REQ_GAP)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404 and allow_404:
                return None
            last_err = f"HTTP {e.code}"
            if e.code in (403,) and attempt == 0:
                print(f"  ! 403 from SEC for {url} - check SEC_USER_AGENT", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # network hiccup
            last_err = str(e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"request failed: {url} ({last_err})")


# ---------------------------------------------------------------- dates
def d(s):
    return dt.date.fromisoformat(s)


def cal_quarter(end):
    """Map a period-end date to the nearest calendar quarter end, e.g. 2026-08-31 -> '2026Q3'."""
    best = None
    for y in (end.year - 1, end.year, end.year + 1):
        for q, (m, dd) in enumerate(((3, 31), (6, 30), (9, 30), (12, 31)), start=1):
            qe = dt.date(y, m, dd)
            gap = abs((end - qe).days)
            if best is None or gap < best[0]:
                best = (gap, f"{y}Q{q}")
    return best[1]


def cq_end(label):
    y, q = int(label[:4]), int(label[-1])
    return dt.date(y, (3, 6, 9, 12)[q - 1], (31, 30, 30, 31)[q - 1])


def cq_shift(label, n):
    y, q = int(label[:4]), int(label[-1])
    idx = y * 4 + (q - 1) + n
    return f"{idx // 4}Q{idx % 4 + 1}"


# ---------------------------------------------------------------- fact helpers
def usd_facts(cf, tag, taxonomy="us-gaap"):
    try:
        return cf["facts"][taxonomy][tag]["units"]["USD"]
    except (KeyError, TypeError):
        return []


def period_ends(cf, tags):
    """All fiscal period end dates seen in duration facts (balance-sheet dates)."""
    ends = set()
    for tag in tags:
        for f in usd_facts(cf, tag):
            if f.get("start") and f.get("form") in FORMS:
                ends.add(f["end"])
    return ends


def on_period_end(series, ends):
    """Drop instants that are not period ends (e.g. subsequent-event dates)."""
    if not ends:
        return series
    ok = [d(e) for e in ends]
    return [p for p in series if any(abs((d(p["end"]) - e).days) <= 3 for e in ok)]


def diagnose(cf):
    """Candidate backlog-like tags in any taxonomy, for manual override later."""
    out = []
    for tax, tags in (cf.get("facts") or {}).items():
        for tag, body in tags.items():
            if not any(k in tag.lower() for k in DIAG_KEYS):
                continue
            facts = (body.get("units") or {}).get("USD") or []
            facts = [f for f in facts if f.get("form") in FORMS]
            if not facts:
                continue
            last = max(facts, key=lambda f: (f["end"], f.get("filed", "")))
            out.append({"tag": f"{tax}:{tag}", "n": len(facts), "last_end": last["end"],
                        "last_val": last["val"], "instant": not last.get("start")})
    out.sort(key=lambda x: x["last_end"], reverse=True)
    return out[:10]


def dedupe(facts):
    """Same period reported in several filings -> keep the most recently filed value."""
    best = {}
    for f in facts:
        if f.get("form") not in FORMS:
            continue
        key = (f.get("start"), f["end"])
        if key not in best or f.get("filed", "") > best[key].get("filed", ""):
            best[key] = f
    return list(best.values())


def instant_series(facts):
    out = {}
    for f in dedupe(facts):
        if f.get("start"):
            continue
        out[f["end"]] = f
    return [out[k] for k in sorted(out)]


def quarterly_series(facts):
    """
    Build discrete quarterly values from duration facts.
    Direct 3-month facts are used as-is; year-to-date chains that share a start
    date are differenced (6M-3M, 9M-6M, 12M-9M) to recover the missing quarters.
    """
    groups = {}
    for f in dedupe(facts):
        if not f.get("start"):
            continue
        groups.setdefault(f["start"], []).append(f)
    quarters = {}  # end -> record
    for start, items in groups.items():
        items.sort(key=lambda x: x["end"])
        prev = None
        for f in items:
            if prev is None:
                dur = (d(f["end"]) - d(start)).days
                if 80 <= dur <= 100:
                    rec = {"end": f["end"], "val": f["val"], "how": "direct",
                           "accn": f.get("accn"), "filed": f.get("filed"), "form": f.get("form")}
                    quarters[f["end"]] = rec
            else:
                gap = (d(f["end"]) - d(prev["end"])).days
                if 80 <= gap <= 100:
                    rec = {"end": f["end"], "val": f["val"] - prev["val"], "how": "derived",
                           "accn": f.get("accn"), "filed": f.get("filed"), "form": f.get("form")}
                    old = quarters.get(f["end"])
                    if old is None or old["how"] != "direct":
                        quarters[f["end"]] = rec
            prev = f
    return [quarters[k] for k in sorted(quarters)]


def merged_quarterly(cf, tags):
    """Fill quarters from tags in priority order (companies switch tags over time)."""
    merged, used = {}, []
    for tag in tags:
        series = quarterly_series(usd_facts(cf, tag))
        if not series:
            continue
        added = 0
        for rec in series:
            if rec["end"] not in merged:
                rec = dict(rec, tag=tag)
                merged[rec["end"]] = rec
                added += 1
        if added:
            used.append(tag)
    return [merged[k] for k in sorted(merged)], used


def find_point(series, target, lo, hi):
    """Point whose end is between target-hi and target-lo days (closest to target-mid)."""
    best = None
    mid = (lo + hi) / 2
    for p in series:
        gap = (target - d(p["end"])).days
        if lo <= gap <= hi:
            score = abs(gap - mid)
            if best is None or score < best[0]:
                best = (score, p)
    return best[1] if best else None


def ttm_revenue(rev_q, asof):
    eligible = [r for r in rev_q if -10 <= (asof - d(r["end"])).days <= 330]
    eligible.sort(key=lambda r: r["end"], reverse=True)
    if len(eligible) < 4:
        return None
    last4 = eligible[:4]
    if abs((asof - d(last4[0]["end"])).days) > 15:
        return None
    span = (d(last4[0]["end"]) - d(last4[3]["end"])).days
    if not (250 <= span <= 300):
        return None
    return sum(r["val"] for r in last4)


def pct(new, old):
    if old in (None, 0) or new is None:
        return None
    return (new - old) / abs(old)


def slim(p, extra=None):
    o = {"end": p["end"], "val": p["val"], "accn": p.get("accn"), "filed": p.get("filed"), "form": p.get("form")}
    if extra:
        o.update(extra)
    return o


# ---------------------------------------------------------------- main build
def build():
    if not UA or "@" not in UA:
        sys.exit("SEC_USER_AGENT secret is missing or has no email address. "
                 "Example value: 'SJ Market Desk yourname@example.com'")

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    tickers_map = get_json("https://www.sec.gov/files/company_tickers.json")
    t2c, c2t = {}, {}
    for row in tickers_map.values():
        t2c[row["ticker"].upper()] = (int(row["cik_str"]), row["title"])
        c2t.setdefault(int(row["cik_str"]), row["ticker"].upper())

    today = dt.date.today()
    companies, notes = [], []

    for cat in cfg["categories"]:
        for tk in cat["tickers"]:
            tk = tk.upper()
            item = {"ticker": tk, "category": cat["id"], "status": "ok"}
            if tk not in t2c:
                item["status"] = "no_cik"
                companies.append(item)
                notes.append(f"{tk}: SEC ticker list has no CIK")
                continue
            cik, title = t2c[tk]
            item.update({"cik": cik, "name": title})
            cf = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", allow_404=True)
            if not cf or "us-gaap" not in cf.get("facts", {}):
                item["status"] = "no_usgaap"
                companies.append(item)
                notes.append(f"{tk}: no us-gaap XBRL facts (foreign filer or new listing)")
                continue

            override = (cfg.get("rpo_overrides") or {}).get(tk)
            if override and ":" in override:
                tax, tag = override.split(":", 1)
            else:
                tax, tag = "us-gaap", RPO_TAG
            item["rpo_tag"] = f"{tax}:{tag}"
            rpo = instant_series(usd_facts(cf, tag, tax))
            rpo = on_period_end(rpo, period_ends(cf, REV_TAGS))
            rev_q, rev_tags = merged_quarterly(cf, REV_TAGS)
            item["rev_tags"] = rev_tags

            if rpo and (today - d(rpo[-1]["end"])).days > STALE_DAYS:
                item["status"] = "stale"
                item["last_rpo"] = slim(rpo[-1])
            if not rpo:
                item["status"] = "no_rpo"
            if item["status"] != "ok":
                item["diag"] = diagnose(cf)
            else:
                last = rpo[-1]
                asof = d(last["end"])
                prev_q = find_point(rpo, asof, 60, 120)
                prev_y = find_point(rpo, asof, 335, 395)
                ttm = ttm_revenue(rev_q, asof)
                item["latest"] = slim(last, {"cq": cal_quarter(asof)})
                item["qoq"] = pct(last["val"], prev_q["val"] if prev_q else None)
                item["yoy"] = pct(last["val"], prev_y["val"] if prev_y else None)
                item["ttm_rev"] = ttm
                item["coverage_years"] = (last["val"] / ttm) if ttm and ttm > 0 else None
                item["stale_days"] = (today - asof).days
                item["rpo"] = [slim(p, {"cq": cal_quarter(d(p["end"]))}) for p in rpo[-KEEP_POINTS:]]
            item["revenue_q"] = [slim(r, {"how": r["how"], "cq": cal_quarter(d(r["end"]))})
                                 for r in rev_q[-KEEP_POINTS:]]

            if tk in [x.upper() for x in cfg.get("capex_group", [])]:
                cap_q, cap_tags = merged_quarterly(cf, CAPEX_TAGS)
                item["capex_q"] = [slim(r, {"how": r["how"], "cq": cal_quarter(d(r["end"]))})
                                   for r in cap_q[-KEEP_POINTS:]]
                item["capex_tags"] = cap_tags
            companies.append(item)
            print(f"  {tk:6s} {item['status']}")

    aggregate = build_aggregate(companies, cfg)
    capex = build_capex(companies, cfg)
    screener = build_screener(cfg, c2t, {c["ticker"] for c in companies}, today)

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "SEC EDGAR XBRL API (companyfacts, frames)",
        "status": "ok",
        "categories": [c["id"] for c in cfg["categories"]],
        "companies": companies,
        "aggregate": aggregate,
        "capex": capex,
        "screener": screener,
        "notes": notes,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")


def by_cq(series):
    """One value per calendar quarter (latest end wins)."""
    m = {}
    for p in series or []:
        cq = p["cq"]
        if cq not in m or p["end"] > m[cq]["end"]:
            m[cq] = p
    return m


def build_aggregate(companies, cfg):
    groups = {"all": [c for c in companies if c.get("rpo") and c["status"] == "ok"]}
    for cat in cfg["categories"]:
        groups[cat["id"]] = [c for c in groups["all"] if c["category"] == cat["id"]]
    result = {}
    for gid, members in groups.items():
        if not members:
            result[gid] = []
            continue
        maps = {c["ticker"]: by_cq(c["rpo"]) for c in members}
        all_cq = sorted({cq for m in maps.values() for cq in m})[-10:]
        rows = []
        for cq in all_cq:
            have = [t for t, m in maps.items() if cq in m]
            # comparable base: members that reported both this quarter and the prior one
            prev = cq_shift(cq, -1)
            both = [t for t in have if prev in maps[t]]
            rows.append({
                "cq": cq,
                "sum": sum(maps[t][cq]["val"] for t in have),
                "n": len(have),
                "of": len(members),
                "same_base_qoq": pct(sum(maps[t][cq]["val"] for t in both),
                                     sum(maps[t][prev]["val"] for t in both)) if both else None,
                "same_base_n": len(both),
            })
        result[gid] = rows
    return result


def build_capex(companies, cfg):
    group = [c for c in companies if c.get("capex_q")]
    maps = {c["ticker"]: by_cq(c["capex_q"]) for c in group}
    all_cq = sorted({cq for m in maps.values() for cq in m})[-10:]
    rows = []
    for cq in all_cq:
        per = {t: m[cq]["val"] for t, m in maps.items() if cq in m}
        rows.append({"cq": cq, "by": per, "sum": sum(per.values()), "n": len(per), "of": len(group)})
    return rows


def build_screener(cfg, c2t, universe_tickers, today):
    """Whole-market RPO ranking via XBRL frames (one call per calendar quarter)."""
    min_usd = cfg.get("screener_min_usd", 1_000_000_000)
    last_closed = cal_quarter(today)
    if cq_end(last_closed) >= today:
        last_closed = cq_shift(last_closed, -1)
    frames = {}
    for i in range(0, 7):
        cq = cq_shift(last_closed, -i)
        url = (f"https://data.sec.gov/api/xbrl/frames/us-gaap/{RPO_TAG}/USD/"
               f"CY{cq[:4]}Q{cq[-1]}I.json")
        js = get_json(url, allow_404=True)
        if js and js.get("data"):
            frames[cq] = {int(r["cik"]): r for r in js["data"]}
    if not frames:
        return {"status": "empty"}
    order = sorted(frames)
    # newest quarter with reasonable coverage vs the quarter before it
    pick = order[-1]
    for cq in reversed(order):
        prev = cq_shift(cq, -1)
        if prev not in frames or len(frames[cq]) >= 0.8 * len(frames[prev]):
            pick = cq
            break
    cur, q1, y1 = frames[pick], frames.get(cq_shift(pick, -1), {}), frames.get(cq_shift(pick, -4), {})
    rows = []
    for cik, r in cur.items():
        if r["val"] < min_usd:
            continue
        tk = c2t.get(cik)
        base_q = q1[cik]["val"] if cik in q1 else None
        base_y = y1[cik]["val"] if cik in y1 else None
        rows.append({
            "cik": cik, "ticker": tk, "name": r.get("entityName"),
            "end": r["end"], "val": r["val"], "accn": r.get("accn"),
            "qoq": pct(r["val"], base_q) if base_q and base_q >= YOY_BASE_MIN else None,
            "yoy": pct(r["val"], base_y) if base_y and base_y >= YOY_BASE_MIN else None,
            "new": base_y is None or base_y < YOY_BASE_MIN,
            "in_universe": tk in universe_tickers if tk else False,
        })
    rows.sort(key=lambda x: (x["yoy"] is None, -(x["yoy"] or 0)))
    return {
        "status": "ok",
        "quarter": pick,
        "counts": {cq: len(frames[cq]) for cq in order},
        "partial_newer": [cq for cq in order if cq > pick],
        "min_usd": min_usd,
        "yoy_base_min": YOY_BASE_MIN,
        "rows": rows,
    }


if __name__ == "__main__":
    build()

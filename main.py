#!/usr/bin/env python3
"""
Daily market newsletter — facts come from code, not from a language model.

Design principles (non-negotiable):
  1. Every IPO filing, index change, and price is pulled deterministically from
     an authoritative source. No LLM is used anywhere in this pipeline.
  2. Every section reports one of THREE states, and the email shows which:
        OK            -> real data, taken verbatim from the source
        NO_UPDATES    -> source was reached successfully and had nothing new
        CHECK_FAILED  -> the source could NOT be reached/parsed. This is shown
                         as a warning with a manual-check link. It is NEVER
                         silently collapsed into "no updates", because a failed
                         check is not the same as a verified absence of news.
  3. Anything this script cannot verify is printed as "N/A" or CHECK_FAILED,
     never guessed.

Sections:
  1. EDGAR registration filings (S-1, S-1/A, F-1, F-1/A, 424B4) — IPO pipeline
  2. Index additions/deletions — S&P DJI (PR Newswire), Nasdaq (GlobeNewswire),
     FTSE Russell (official 2026 calendar + best-effort notices scrape)
  3. Market snapshot — last fully completed regular session (Yahoo chart API)

Environment variables:
  GMAIL_ADDRESS        sender Gmail address                       (required to send)
  GMAIL_APP_PASSWORD   Gmail app password                         (required to send)
  NEWSLETTER_TO        comma-separated recipients (default: GMAIL_ADDRESS)
  SEC_CONTACT          identification for SEC requests, e.g.
                       "Danny Le danny@example.com" (SEC fair-access policy
                       requires a declared User-Agent; default uses GMAIL_ADDRESS)

Usage:
  python main.py             # build and send the email
  python main.py --dry-run   # build newsletter.html locally, print summary, no email
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

EASTERN = ZoneInfo("America/New_York")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NEWSLETTER_TO = [
    a.strip() for a in os.environ.get("NEWSLETTER_TO", GMAIL_ADDRESS).split(",") if a.strip()
]
SEC_CONTACT = os.environ.get("SEC_CONTACT", f"Personal market newsletter {GMAIL_ADDRESS}")

# SEC fair-access policy: declared User-Agent, <=10 requests/second.
SEC_HEADERS = {"User-Agent": SEC_CONTACT, "Accept-Encoding": "gzip, deflate"}
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ----- Section 1: EDGAR --------------------------------------------------- #
# Daily master index: pipe-delimited list of EVERY filing accepted that day.
# Stable since 1994; rebuilt each evening, so yesterday's file is final by morning.
EDGAR_DAILY_MASTER = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/master.{ymd}.idx"
# Per-company filing history (official documented JSON API) — used to tell
# first-time registrants (true IPO candidates) from already-public companies
# filing shelf/secondary registrations.
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EDGAR_FILING_INDEX = "https://www.sec.gov/Archives/{path}"

IPO_FORMS = {"S-1", "S-1/A", "F-1", "F-1/A", "424B4"}
FORM_SORT_ORDER = {"424B4": 0, "S-1": 1, "F-1": 2, "S-1/A": 3, "F-1/A": 4}
# Periodic-report forms that prove a filer is ALREADY a public reporting company.
REPORTING_FORMS = {"10-K", "10-Q", "8-K", "20-F", "40-F", "6-K"}
MAX_FIRST_TIME_LOOKUPS = 40  # safety cap on data.sec.gov calls per run

# ----- Section 2: index providers ----------------------------------------- #
# S&P DJI distributes add/delete announcements through PR Newswire
# ("SOURCE S&P Dow Jones Indices"); press.spglobal.com is the canonical archive.
SPDJI_PRN_LIST = "https://www.prnewswire.com/news/s%26p-dow-jones-indices/"
SPDJI_MANUAL_LINKS = [
    ("press.spglobal.com (canonical archive)", "https://press.spglobal.com/"),
    ("S&P DJI media center", "https://www.spglobal.com/spdji/en/media-center/news-announcements/"),
]
SPDJI_TITLE_RE = re.compile(
    r"(set to join|set to be removed|will replace|to replace|changes to the s&p"
    r"|index changes|quarterly rebalance|annual rebalance)",
    re.I,
)

# Nasdaq distributes index announcements through GlobeNewswire (Source: Nasdaq, Inc.,
# organization id 6948 — visible in every Nasdaq release URL on globenewswire.com).
NASDAQ_GNW_RSS_CANDIDATES = [
    "https://www.globenewswire.com/RssFeed/organization/6948/feedTitle/Nasdaq",
    "https://www.globenewswire.com/en/RssFeed/organization/6948/feedTitle/Nasdaq",
]
NASDAQ_MANUAL_LINKS = [
    ("Nasdaq Global Index Watch", "https://indexes.nasdaqomx.com/"),
    ("Nasdaq releases on GlobeNewswire",
     "https://www.globenewswire.com/en/search/organization/Nasdaq%2C%2520Inc%2E"),
]
NASDAQ_TITLE_RE = re.compile(
    r"(nasdaq-100|nasdaq next generation|index).*?(change|rebalance|reconstitution|replace)"
    r"|(annual|quarterly) changes",
    re.I,
)

# FTSE Russell: official, published 2026 calendar (hard-coded from the
# 2026-03-02 FTSE Russell press release and the June recon press release).
# Reconstitution is SEMI-ANNUAL starting 2026 (June + December), plus quarterly
# IPO additions (March, June, September, December).
RUSSELL_CALENDAR_2026 = {
    date(2026, 4, 30): "Rank Day — June recon membership determined from market caps at today's close.",
    date(2026, 5, 22): "Preliminary June recon addition & deletion lists posted after 6pm ET.",
    date(2026, 5, 29): "Updated June recon preliminary lists posted after 6pm ET.",
    date(2026, 6, 5):  "Updated June recon preliminary lists posted after 6pm ET.",
    date(2026, 6, 8):  "Lock-down period begins — recon membership updates considered final.",
    date(2026, 6, 12): "Updated June recon preliminary lists posted after 6pm ET.",
    date(2026, 6, 18): "Final pre-recon membership lists posted after 6pm ET.",
    date(2026, 6, 26): "June reconstitution EFFECTIVE after US close; new indexes live Mon Jun 29 open.",
    date(2026, 10, 30): "Rank Day for the December (semi-annual) reconstitution.",
    date(2026, 12, 11): "December reconstitution effective after US close (second Friday of December).",
}
RUSSELL_NOTICES_PAGE = "https://research.ftserussell.com/products/index-notices"
RUSSELL_RECON_LISTS = "https://www.ftserussell.com/resources/russell-reconstitution"
RUSSELL_MANUAL_LINKS = [
    ("FTSE Russell index notices", RUSSELL_NOTICES_PAGE),
    ("Russell reconstitution lists", RUSSELL_RECON_LISTS),
]

# ----- Section 3: market snapshot ------------------------------------------ #
YAHOO_CHART = "https://{host}/v8/finance/chart/{symbol}?range=10d&interval=1d"
YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
BENCHMARKS = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq Composite"),
    ("^DJI", "Dow Jones Industrial Average"),
    ("^RUT", "Russell 2000"),
    ("^TNX", "10-Year Treasury Yield"),
]

NO_UPDATES_IPO = "No new IPO filings or pricings today."
NO_UPDATES_INDEX = "No index additions or deletions announced today."

log = logging.getLogger("newsletter")

# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def http_get(url: str, headers: dict, *, retries: int = 3, timeout: int = 20,
             backoff: float = 1.5) -> requests.Response:
    """GET with retries + exponential backoff. Raises on final failure."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            log.info("GET %s (attempt %d/%d)", url, attempt, retries)
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            log.warning("HTTP %s from %s", resp.status_code, url)
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
        except requests.RequestException as exc:
            log.warning("Request error for %s: %s", url, exc)
            last_exc = exc
        time.sleep(backoff ** attempt)
    raise RuntimeError(f"GET failed after {retries} attempts: {url} ({last_exc})")


def now_eastern() -> datetime:
    return datetime.now(tz=EASTERN)


def previous_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def lookback_days(today: date) -> int:
    """Announcement window: 1 day normally, 3 over a weekend (Monday run)."""
    return 3 if today.weekday() == 0 else 1


@dataclass
class SectionResult:
    """Three-state result for every data source."""
    status: str                      # "OK" | "NO_UPDATES" | "CHECK_FAILED"
    items: list = field(default_factory=list)
    error: str = ""
    note: str = ""


# --------------------------------------------------------------------------- #
# Section 1 — EDGAR IPO / registration tracker
# --------------------------------------------------------------------------- #


def fetch_edgar_daily_filings(run_day: date) -> tuple[SectionResult, date | None]:
    """
    Pull the previous business day's complete EDGAR daily master index and keep
    S-1 / S-1/A / F-1 / F-1/A / 424B4 rows VERBATIM. The daily master index is
    pipe-delimited:  CIK|Company Name|Form Type|Date Filed|File Name
    Walks back up to 7 calendar days to skip federal holidays (404 = no index).
    """
    target = previous_business_day(run_day)
    for _ in range(7):
        quarter = (target.month - 1) // 3 + 1
        url = EDGAR_DAILY_MASTER.format(y=target.year, q=quarter, ymd=target.strftime("%Y%m%d"))
        try:
            resp = http_get(url, SEC_HEADERS, retries=2)
        except RuntimeError as exc:
            if "HTTP 403" in str(exc):
                return SectionResult("CHECK_FAILED", error=f"EDGAR refused the request ({exc}). "
                                     "Check the SEC_CONTACT User-Agent."), None
            log.info("No daily index for %s (likely a federal holiday); stepping back.", target)
            target = previous_business_day(target)
            continue

        filings = parse_master_idx(resp.text)
        hits = [f for f in filings if f["form"] in IPO_FORMS]
        hits.sort(key=lambda f: (FORM_SORT_ORDER.get(f["form"], 9), f["company"]))
        log.info("EDGAR %s: %d total filings, %d in IPO form set.", target, len(filings), len(hits))

        annotate_first_time_filers(hits)
        status = "OK" if hits else "NO_UPDATES"
        return SectionResult(status, items=hits), target

    return SectionResult("CHECK_FAILED",
                         error="No EDGAR daily index found in the last 7 days."), None


def parse_master_idx(text: str) -> list[dict]:
    """Parse the pipe-delimited daily master index. Header ends at the dashed line."""
    out: list[dict] = []
    in_body = False
    for line in text.splitlines():
        if not in_body:
            if line.startswith("---"):
                in_body = True
            continue
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, company, form, filed, path = (p.strip() for p in parts)
        if not cik.isdigit():
            continue
        index_page = EDGAR_FILING_INDEX.format(path=path).replace(".txt", "-index.htm")
        out.append({"cik": int(cik), "company": company, "form": form,
                    "filed": filed, "url": index_page})
    return out


def annotate_first_time_filers(filings: list[dict]) -> None:
    """
    Honesty check: an S-1/F-1/424B4 is NOT automatically an IPO — public
    companies file the same forms for shelf and secondary offerings. A filer
    with NO prior periodic reports (10-K/10-Q/8-K/20-F/40-F/6-K) on EDGAR is a
    first-time registrant, i.e. a genuine IPO candidate. Deterministic check
    against the SEC's official submissions API; "unknown" on any failure.
    """
    cache: dict[int, str] = {}
    lookups = 0
    for f in filings:
        cik = f["cik"]
        if cik in cache:
            f["first_time"] = cache[cik]
            continue
        if lookups >= MAX_FIRST_TIME_LOOKUPS:
            f["first_time"] = "unknown (lookup cap)"
            continue
        lookups += 1
        try:
            resp = http_get(EDGAR_SUBMISSIONS.format(cik=cik), SEC_HEADERS, retries=2, timeout=15)
            forms = set(resp.json().get("filings", {}).get("recent", {}).get("form", []))
            verdict = "no" if forms & REPORTING_FORMS else "YES"
        except Exception as exc:  # noqa: BLE001 — any failure means "unknown", never a guess
            log.warning("submissions lookup failed for CIK %s: %s", cik, exc)
            verdict = "unknown"
        cache[cik] = verdict
        f["first_time"] = verdict
        time.sleep(0.12)  # stay far under SEC's 10 req/s limit


# --------------------------------------------------------------------------- #
# Section 2a — S&P DJI (PR Newswire)
# --------------------------------------------------------------------------- #


def fetch_spdji_announcements(run_day: date) -> SectionResult:
    window = lookback_days(run_day)
    cutoff = run_day - timedelta(days=window)
    try:
        resp = http_get(SPDJI_PRN_LIST, BROWSER_HEADERS)
    except RuntimeError as exc:
        return SectionResult("CHECK_FAILED", error=str(exc))

    soup = BeautifulSoup(resp.text, "html.parser")
    items: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news-releases/" not in href:
            continue
        title = a.get_text(" ", strip=True)
        if not title or not SPDJI_TITLE_RE.search(title) or title in seen:
            continue
        # Date stamp sits in the surrounding card; grab the nearest "Mon DD, YYYY".
        block = a.find_parent(["div", "article", "li"]) or a
        m = re.search(r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})", block.get_text(" ", strip=True))
        rel_date = None
        if m:
            try:
                rel_date = datetime.strptime(m.group(1), "%b %d, %Y").date()
            except ValueError:
                rel_date = None
        if rel_date and rel_date < cutoff:
            continue
        seen.add(title)
        full_url = href if href.startswith("http") else "https://www.prnewswire.com" + href
        items.append({"title": title, "date": rel_date.isoformat() if rel_date else "see release",
                      "url": full_url, "changes": []})

    log.info("S&P DJI: %d matching releases in the last %d day(s).", len(items), window)
    for item in items[:5]:  # parse the structured table inside each release
        item["changes"] = parse_prn_changes_table(item["url"])
    return SectionResult("OK" if items else "NO_UPDATES", items=items,
                         note=f"window: last {window} day(s)")


def parse_prn_changes_table(url: str) -> list[dict]:
    """
    S&P DJI releases embed a table: Effective Date | Index Name | Action |
    Company Name | Ticker | GICS Sector. All values are carried over VERBATIM.
    Returns [] if the table can't be parsed — the headline + link still go out.
    """
    try:
        resp = http_get(url, BROWSER_HEADERS, retries=2)
    except RuntimeError as exc:
        log.warning("Could not fetch release body %s: %s", url, exc)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    for table in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                for tr in table.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if not rows or "action" not in " ".join(rows[0]).lower():
            continue
        header = [h.lower() for h in rows[0]]

        def col(*names: str) -> int | None:
            for i, h in enumerate(header):
                if any(n in h for n in names):
                    return i
            return None

        idx = {"effective": col("effective"), "index": col("index"),
               "action": col("action"), "company": col("company"),
               "ticker": col("ticker"), "sector": col("gics", "sector")}
        out = []
        for r in rows[1:]:
            def cell(key: str) -> str:
                i = idx[key]
                return r[i] if i is not None and i < len(r) else ""
            if cell("action") or cell("company"):
                out.append({k: cell(k) for k in idx})
        if out:
            log.info("Parsed %d change rows from %s", len(out), url)
            return out
    log.warning("No changes table found in %s — reporting headline only.", url)
    return []


# --------------------------------------------------------------------------- #
# Section 2b — Nasdaq (GlobeNewswire)
# --------------------------------------------------------------------------- #


def fetch_nasdaq_announcements(run_day: date) -> SectionResult:
    window = lookback_days(run_day)
    cutoff = run_day - timedelta(days=window)
    feed_xml, used = None, None
    for candidate in NASDAQ_GNW_RSS_CANDIDATES:
        try:
            resp = http_get(candidate, BROWSER_HEADERS, retries=2)
            if "<rss" in resp.text[:500] or "<feed" in resp.text[:500]:
                feed_xml, used = resp.text, candidate
                break
            log.warning("Candidate feed %s did not return RSS/Atom.", candidate)
        except RuntimeError as exc:
            log.warning("Candidate feed %s failed: %s", candidate, exc)
    if feed_xml is None:
        return SectionResult("CHECK_FAILED",
                             error="GlobeNewswire RSS for Nasdaq, Inc. unreachable.")

    items: list[dict] = []
    try:
        root = ET.fromstring(feed_xml)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = (it.findtext("pubDate") or "").strip()
            pub_date = None
            for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
                try:
                    pub_date = datetime.strptime(pub, fmt).date()
                    break
                except ValueError:
                    continue
            if not title or not NASDAQ_TITLE_RE.search(title):
                continue
            if pub_date and pub_date < cutoff:
                continue
            items.append({"title": title, "date": pub_date.isoformat() if pub_date else pub,
                          "url": link, "detail": []})
    except ET.ParseError as exc:
        return SectionResult("CHECK_FAILED", error=f"Could not parse feed {used}: {exc}")

    log.info("Nasdaq: %d matching releases in the last %d day(s).", len(items), window)
    for item in items[:5]:
        item["detail"] = extract_nasdaq_change_sentences(item["url"])
    return SectionResult("OK" if items else "NO_UPDATES", items=items,
                         note=f"window: last {window} day(s)")


# Corporate suffixes whose periods must NOT end a sentence ("Astera Labs, Inc.
# (Nasdaq: ALAB) ... will be added"). Case-sensitive on purpose — releases
# always capitalize these.
_ABBREVS = ["Inc.", "Corp.", "Co.", "Cos.", "Ltd.", "L.P.", "N.V.", "S.A.",
            "S.p.A.", "plc.", "PLC.", "Jr.", "Sr.", "No.", "U.S.", "vs.", "Cl."]
_CHANGE_PHRASES = ("will be added", "will be removed", "will replace",
                   "become effective", "effective prior to market open")


def _split_sentences(text: str) -> list[str]:
    """Sentence split that survives 'Inc.', 'N.V.' etc. inside company names."""
    protected = text
    for ab in _ABBREVS:
        protected = protected.replace(ab, ab.replace(".", "\x00"))
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [p.replace("\x00", ".").strip() for p in parts if p.strip()]


def extract_nasdaq_change_sentences(url: str) -> list[str]:
    """
    Pull the literal sentences naming additions/removals and the effective date
    from the release body. Nasdaq's format lists the companies BEFORE the verb
    ("X, Inc. (Nasdaq: X), and Y, Inc. (Nasdaq: Y) will be added to the
    Index."), so we capture whole sentences containing the key phrases.
    Verbatim extraction only — if nothing matches, nothing is invented and the
    headline + link still go out.
    """
    try:
        resp = http_get(url, BROWSER_HEADERS, retries=2)
    except RuntimeError as exc:
        log.warning("Could not fetch Nasdaq release %s: %s", url, exc)
        return []
    text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    out: list[str] = []
    seen: set[str] = set()
    for sent in _split_sentences(text):
        low = sent.lower()
        if (any(p in low for p in _CHANGE_PHRASES)
                and sent not in seen and len(sent) <= 700):  # >700 = split failed; keep headline only
            seen.add(sent)
            out.append(sent)
        if len(out) >= 8:  # safety cap against boilerplate-heavy pages
            break
    return out


# --------------------------------------------------------------------------- #
# Section 2c — FTSE Russell (official calendar + best-effort notices)
# --------------------------------------------------------------------------- #


def fetch_russell(run_day: date) -> SectionResult:
    """
    Two layers:
      (1) Deterministic: the OFFICIAL published 2026 recon calendar. Flags any
          calendar event in the lookback window or today. Cannot fail and
          cannot hallucinate — the dates come from FTSE Russell's own schedule
          press release.
      (2) Best-effort: scrape the index-notices page for fresh notices. The
          listing is a JS app, so this often degrades to CHECK_FAILED — which
          is reported honestly with manual links, NOT as "no updates".
    """
    window = lookback_days(run_day)
    items: list[dict] = []
    for offset in range(-window, 1):  # past `window` days through today
        d = run_day + timedelta(days=offset)
        if d in RUSSELL_CALENDAR_2026:
            when = "TODAY" if offset == 0 else d.strftime("%a %b %d")
            items.append({"title": f"[Official recon calendar — {when}] {RUSSELL_CALENDAR_2026[d]}",
                          "date": d.isoformat(), "url": RUSSELL_RECON_LISTS, "detail": []})

    note = ""
    try:
        resp = http_get(RUSSELL_NOTICES_PAGE, BROWSER_HEADERS, retries=2)
        soup = BeautifulSoup(resp.text, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            if "getnotice" not in a["href"]:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            href = a["href"]
            full = href if href.startswith("http") else "https://research.ftserussell.com" + href
            items.append({"title": f"[Index notice] {title}", "date": "see notice",
                          "url": full, "detail": []})
            found += 1
            if found >= 8:
                break
        note = (f"notices page scraped, {found} notice link(s) found"
                if found else "notices page reachable but no notice links extracted "
                              "(listing is JavaScript-rendered) — check manually")
        log.info("Russell notices scrape: %s", note)
    except RuntimeError as exc:
        note = f"notices page check failed ({exc}) — calendar layer above is still authoritative"
        log.warning("Russell notices scrape failed: %s", exc)

    if items:
        return SectionResult("OK", items=items, note=note)
    if note.startswith("notices page check failed"):
        return SectionResult("CHECK_FAILED", error=note,
                             note="No official calendar events in window either.")
    return SectionResult("NO_UPDATES", note=note)


# --------------------------------------------------------------------------- #
# Section 3 — market snapshot (last fully completed regular session)
# --------------------------------------------------------------------------- #


def fetch_benchmark(symbol: str, run_day: date) -> dict:
    """
    Yahoo Finance chart endpoint, called directly with a browser User-Agent
    (yfinance is unreliable on GitHub Actions). Selects the LAST daily bar
    strictly before today (US/Eastern) with both open and close present — i.e.
    the last fully completed regular session; never a partial or pre-market bar.

    Reports BOTH conventions:
      session_pct  = open -> close of that session (the spec's definition)
      cc_pct       = previous close -> close (the convention news headlines use)
    """
    data = None
    for host in YAHOO_HOSTS:
        url = YAHOO_CHART.format(host=host, symbol=requests.utils.quote(symbol))
        try:
            data = http_get(url, BROWSER_HEADERS, retries=2).json()
            break
        except (RuntimeError, ValueError) as exc:
            log.warning("Yahoo %s via %s failed: %s", symbol, host, exc)
    if data is None:
        return {"symbol": symbol, "ok": False}

    try:
        result = data["chart"]["result"][0]
        ts = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        opens, closes = quote["open"], quote["close"]
        bars = []
        for i, t in enumerate(ts):
            bar_day = datetime.fromtimestamp(t, tz=EASTERN).date()
            if bar_day < run_day and opens[i] is not None and closes[i] is not None:
                bars.append((bar_day, opens[i], closes[i]))
        if not bars:
            return {"symbol": symbol, "ok": False}
        session_day, o, c = bars[-1]
        prev_close = bars[-2][2] if len(bars) >= 2 else None
        return {
            "symbol": symbol, "ok": True, "session_day": session_day,
            "open": o, "close": c,
            "session_chg": c - o, "session_pct": (c - o) / o * 100 if o else None,
            "cc_chg": (c - prev_close) if prev_close else None,
            "cc_pct": (c - prev_close) / prev_close * 100 if prev_close else None,
        }
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("Unexpected Yahoo payload for %s: %s", symbol, exc)
        return {"symbol": symbol, "ok": False}


def fetch_snapshot(run_day: date) -> SectionResult:
    rows = []
    for symbol, name in BENCHMARKS:
        r = fetch_benchmark(symbol, run_day)
        r["name"] = name
        rows.append(r)
        time.sleep(0.4)
    ok = any(r["ok"] for r in rows)
    return SectionResult("OK" if ok else "CHECK_FAILED", items=rows,
                         error="" if ok else "All benchmark fetches failed.")


# --------------------------------------------------------------------------- #
# Email rendering (template-generated — no LLM anywhere)
# --------------------------------------------------------------------------- #

BADGES = {
    "OK": ("#e8f5e9", "#1b5e20", "UPDATES"),
    "NO_UPDATES": ("#f5f5f5", "#616161", "NO NEW UPDATES"),
    "CHECK_FAILED": ("#fff8e1", "#b26a00", "SOURCE CHECK FAILED"),
}


def esc(s) -> str:
    return html.escape(str(s))


def badge(status: str) -> str:
    bg, fg, label = BADGES[status]
    return (f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;'
            f'font-size:11px;font-weight:bold;letter-spacing:.5px;">{label}</span>')


def section_header(title: str, status: str) -> str:
    return (f'<h2 style="font-size:16px;margin:28px 0 8px;border-bottom:2px solid #14365D;'
            f'padding-bottom:4px;color:#14365D;">{esc(title)} &nbsp;{badge(status)}</h2>')


def render_failed(res: SectionResult, manual_links: list[tuple[str, str]]) -> str:
    links = " · ".join(f'<a href="{esc(u)}" style="color:#115788;">{esc(n)}</a>'
                       for n, u in manual_links)
    return (f'<p style="margin:6px 0;color:#b26a00;"><b>Could not verify this source '
            f'this morning</b> ({esc(res.error)}). This is <i>not</i> a confirmation that '
            f'nothing happened — check manually: {links}</p>')


def render_ipo(res: SectionResult, edgar_day: date | None) -> str:
    h = section_header(
        f"IPO pipeline — EDGAR registration filings"
        + (f" ({edgar_day:%a %b %d})" if edgar_day else ""), res.status)
    if res.status == "CHECK_FAILED":
        return h + render_failed(res, [("EDGAR full-text search", "https://efts.sec.gov/LATEST/search-index?q=%22initial%20public%20offering%22&forms=424B4")])
    if res.status == "NO_UPDATES":
        return h + f"<p>{NO_UPDATES_IPO}</p>"
    rows = "".join(
        f'<tr><td style="padding:4px 8px;"><a href="{esc(f["url"])}" style="color:#115788;">'
        f'{esc(f["company"])}</a></td>'
        f'<td style="padding:4px 8px;"><b>{esc(f["form"])}</b></td>'
        f'<td style="padding:4px 8px;">{esc(f["filed"])}</td>'
        f'<td style="padding:4px 8px;text-align:center;">{esc(f.get("first_time", "?"))}</td></tr>'
        for f in res.items)
    note = ('<p style="font-size:12px;color:#616161;margin:6px 0;">"First-time filer = YES" '
            'means no prior periodic reports on EDGAR — a genuine IPO candidate. "no" means an '
            'already-public company (likely shelf/secondary registration). 424B4 = final '
            'prospectus at pricing — the deal is going effective now.</p>')
    return h + ('<table style="border-collapse:collapse;font-size:13px;width:100%;">'
                '<tr style="background:#14365D;color:#fff;"><th style="padding:4px 8px;text-align:left;">Filer (verbatim from EDGAR)</th>'
                '<th style="padding:4px 8px;text-align:left;">Form</th><th style="padding:4px 8px;text-align:left;">Filed</th>'
                '<th style="padding:4px 8px;">First-time filer?</th></tr>'
                + rows + "</table>" + note)


def render_index_provider(title: str, res: SectionResult,
                          manual_links: list[tuple[str, str]]) -> str:
    h = section_header(title, res.status)
    if res.status == "CHECK_FAILED":
        return h + render_failed(res, manual_links)
    if res.status == "NO_UPDATES":
        return h + f"<p>{NO_UPDATES_INDEX}</p>"
    parts = []
    for it in res.items:
        parts.append(f'<p style="margin:10px 0 2px;"><a href="{esc(it["url"])}" '
                     f'style="color:#115788;font-weight:bold;">{esc(it["title"])}</a> '
                     f'<span style="color:#616161;font-size:12px;">({esc(it["date"])})</span></p>')
        changes = it.get("changes") or []
        if changes:
            rows = "".join(
                f'<tr><td style="padding:3px 8px;">{esc(c["effective"])}</td>'
                f'<td style="padding:3px 8px;">{esc(c["index"])}</td>'
                f'<td style="padding:3px 8px;"><b>{esc(c["action"])}</b></td>'
                f'<td style="padding:3px 8px;">{esc(c["company"])}</td>'
                f'<td style="padding:3px 8px;">{esc(c["ticker"])}</td>'
                f'<td style="padding:3px 8px;">{esc(c["sector"])}</td></tr>' for c in changes)
            parts.append('<table style="border-collapse:collapse;font-size:12px;width:100%;">'
                         '<tr style="background:#eef3f8;color:#14365D;">'
                         '<th style="padding:3px 8px;text-align:left;">Effective</th><th style="padding:3px 8px;text-align:left;">Index</th>'
                         '<th style="padding:3px 8px;text-align:left;">Action</th><th style="padding:3px 8px;text-align:left;">Company</th>'
                         '<th style="padding:3px 8px;text-align:left;">Ticker</th><th style="padding:3px 8px;text-align:left;">GICS</th></tr>'
                         + rows + "</table>")
        for sentence in it.get("detail") or []:
            parts.append(f'<p style="margin:2px 0 2px 12px;font-size:13px;">&ldquo;{esc(sentence)}&rdquo;</p>')
    if res.note:
        parts.append(f'<p style="font-size:11px;color:#9e9e9e;margin:4px 0;">{esc(res.note)}</p>')
    return h + "".join(parts)


def fmt_num(x: float | None, digits: int = 2) -> str:
    return "N/A" if x is None else f"{x:,.{digits}f}"


def render_snapshot(res: SectionResult) -> str:
    day = next((r["session_day"] for r in res.items if r.get("ok")), None)
    h = section_header(
        "Market snapshot — last completed session" + (f" ({day:%a %b %d})" if day else ""),
        res.status)
    if res.status == "CHECK_FAILED":
        return h + render_failed(res, [("Yahoo Finance", "https://finance.yahoo.com/markets/")])
    rows = []
    for r in res.items:
        if not r.get("ok"):
            rows.append(f'<tr><td style="padding:4px 8px;">{esc(r["name"])}</td>'
                        '<td colspan="4" style="padding:4px 8px;color:#9e9e9e;">N/A '
                        '(no clean completed-session data)</td></tr>')
            continue
        if r["symbol"] == "^TNX":  # ^TNX quotes 10x the 10Y yield; show % level and bp moves
            level = f'{r["close"] / 10:.3f}%'
            sess_bp = f'{(r["close"] - r["open"]) * 10:+.1f} bp'
            cc_bp = f'{r["cc_chg"] * 10:+.1f} bp' if r["cc_chg"] is not None else "N/A"
            rows.append(f'<tr><td style="padding:4px 8px;">{esc(r["name"])}</td>'
                        f'<td style="padding:4px 8px;text-align:right;">{level}</td>'
                        f'<td style="padding:4px 8px;text-align:right;">{sess_bp}</td>'
                        f'<td style="padding:4px 8px;text-align:right;">{cc_bp}</td></tr>')
            continue
        color_s = "#1b5e20" if (r["session_pct"] or 0) >= 0 else "#b71c1c"
        color_c = "#1b5e20" if (r["cc_pct"] or 0) >= 0 else "#b71c1c"
        sess_cell = f'{r["session_pct"]:+.2f}%' if r["session_pct"] is not None else "N/A"
        cc_cell = f'{r["cc_pct"]:+.2f}%' if r["cc_pct"] is not None else "N/A"
        rows.append(
            f'<tr><td style="padding:4px 8px;">{esc(r["name"])}</td>'
            f'<td style="padding:4px 8px;text-align:right;">{fmt_num(r["close"])}</td>'
            f'<td style="padding:4px 8px;text-align:right;color:{color_s};">{sess_cell}</td>'
            f'<td style="padding:4px 8px;text-align:right;color:{color_c};">{cc_cell}</td></tr>')
    note = ('<p style="font-size:11px;color:#9e9e9e;margin:4px 0;">Session move = that day\'s '
            'open → close (per spec). vs prior close = the convention news headlines use; it '
            'includes the overnight gap, which the session move excludes.</p>')
    return h + ('<table style="border-collapse:collapse;font-size:13px;width:100%;">'
                '<tr style="background:#14365D;color:#fff;"><th style="padding:4px 8px;text-align:left;">Benchmark</th>'
                '<th style="padding:4px 8px;text-align:right;">Close</th>'
                '<th style="padding:4px 8px;text-align:right;">Session (O→C)</th>'
                '<th style="padding:4px 8px;text-align:right;">vs prior close</th></tr>'
                + "".join(rows) + "</table>" + note)


def build_email(run_dt: datetime, ipo: SectionResult, edgar_day: date | None,
                spdji: SectionResult, nasdaq: SectionResult, russell: SectionResult,
                snap: SectionResult) -> tuple[str, str]:
    """Returns (subject, html_body). The intro line is assembled from data only."""
    n_ipo = len(ipo.items) if ipo.status == "OK" else 0
    n_idx = sum(len(r.items) for r in (spdji, nasdaq, russell) if r.status == "OK")
    spx = next((r for r in snap.items if r.get("symbol") == "^GSPC" and r.get("ok")), None)
    spx_txt = f"SPX {spx['session_pct']:+.2f}%" if spx else "SPX N/A"
    failed = [n for n, r in [("EDGAR", ipo), ("S&P DJI", spdji), ("Nasdaq", nasdaq),
                             ("Russell", russell), ("Markets", snap)] if r.status == "CHECK_FAILED"]

    subject = (f"Market Briefing {run_dt:%a %b %d} — "
               f"{n_ipo} IPO-form filing{'s' if n_ipo != 1 else ''}, "
               f"{n_idx or 'no'} index item{'s' if n_idx != 1 else ''}, {spx_txt}"
               + (f" — CHECK: {', '.join(failed)}" if failed else ""))

    intro_bits = []
    if edgar_day:
        intro_bits.append(f"EDGAR filings cover {edgar_day:%A, %B %d}")
    intro_bits.append(f"{n_ipo} filing(s) in the S-1/F-1/424B4 set")
    intro_bits.append(f"{n_idx} index announcement item(s) in window")
    intro = " · ".join(intro_bits) + "."

    body = f"""<html><body style="font-family:Calibri,Arial,sans-serif;color:#222;max-width:720px;margin:auto;padding:12px;">
<h1 style="font-size:20px;color:#14365D;margin-bottom:2px;">Daily Market Briefing</h1>
<p style="color:#616161;font-size:13px;margin-top:0;">Generated {run_dt:%A, %B %d, %Y at %I:%M %p} ET &nbsp;·&nbsp; {esc(intro)}</p>
{render_ipo(ipo, edgar_day)}
{render_index_provider("Index changes — S&amp;P Dow Jones Indices", spdji, SPDJI_MANUAL_LINKS)}
{render_index_provider("Index changes — Nasdaq", nasdaq, NASDAQ_MANUAL_LINKS)}
{render_index_provider("Index changes — FTSE Russell", russell, RUSSELL_MANUAL_LINKS)}
{render_snapshot(snap)}
<hr style="border:none;border-top:1px solid #ddd;margin:24px 0 8px;">
<p style="font-size:11px;color:#9e9e9e;">Every fact above was pulled deterministically from
SEC EDGAR, PR Newswire (S&amp;P DJI), GlobeNewswire (Nasdaq), FTSE Russell, and the Yahoo Finance
chart API, and is reproduced verbatim. No language model was used to find, summarize, or write
any of it. "Source check failed" means exactly that — verify manually before assuming no news.</p>
</body></html>"""
    return subject, body


# --------------------------------------------------------------------------- #
# Send + main
# --------------------------------------------------------------------------- #


def send_email(subject: str, body_html: str) -> None:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise SystemExit("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — cannot send.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(NEWSLETTER_TO)
    msg.attach(MIMEText(body_html, "html"))
    log.info("Sending to %s via Gmail SMTP ...", NEWSLETTER_TO)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, NEWSLETTER_TO, msg.as_string())
    log.info("Email sent.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily market newsletter (deterministic).")
    parser.add_argument("--dry-run", action="store_true",
                        help="build newsletter.html locally and print a summary; do not send")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    run_dt = now_eastern()
    run_day = run_dt.date()
    log.info("=== Newsletter run starting: %s ET ===", run_dt.strftime("%Y-%m-%d %H:%M"))

    ipo, edgar_day = safe(lambda: fetch_edgar_daily_filings(run_day),
                          fallback=(SectionResult("CHECK_FAILED", error="unhandled error"), None))
    spdji = safe(lambda: fetch_spdji_announcements(run_day))
    nasdaq = safe(lambda: fetch_nasdaq_announcements(run_day))
    russell = safe(lambda: fetch_russell(run_day))
    snap = safe(lambda: fetch_snapshot(run_day))

    for name, res in [("EDGAR", ipo), ("S&P DJI", spdji), ("Nasdaq", nasdaq),
                      ("Russell", russell), ("Snapshot", snap)]:
        log.info("SUMMARY %-8s -> %-12s items=%d %s",
                 name, res.status, len(res.items), res.error or res.note)

    subject, body = build_email(run_dt, ipo, edgar_day, spdji, nasdaq, russell, snap)
    with open("newsletter.html", "w", encoding="utf-8") as fh:
        fh.write(body)
    log.info("Wrote newsletter.html (%d bytes). Subject: %s", len(body), subject)

    if args.dry_run or os.environ.get("DRY_RUN") == "1":
        log.info("Dry run — not sending.")
        return 0
    send_email(subject, body)
    return 0


def safe(fn, fallback=None):
    """One failing section must never kill the whole email."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.exception("Section crashed: %s", exc)
        return fallback if fallback is not None else SectionResult(
            "CHECK_FAILED", error=f"unhandled error: {exc}")


if __name__ == "__main__":
    sys.exit(main())

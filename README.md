# Daily Market Newsletter (deterministic rebuild)

A Python pipeline that emails a morning briefing covering (1) IPO registration
filings, (2) index additions/deletions, and (3) a benchmark snapshot.

**Core rule: facts come from code, not from a language model.** There is no
LLM anywhere in this pipeline — not even for the intro line, which is
assembled from the fetched data by a template. Every company name, ticker,
form type, date, and price is reproduced verbatim from the source or shown as
`N/A`.

## The three states (read this first)

Every section reports exactly one of:

| State | Meaning |
|---|---|
| **UPDATES** | The source was reached and real items were found (shown verbatim). |
| **NO NEW UPDATES** | The source was reached successfully and genuinely had nothing new. This is the *normal* output for index changes most days. |
| **SOURCE CHECK FAILED** | The source could not be reached or parsed. Shown as an amber warning with manual-check links. |

The third state exists because *a failed scrape is not evidence of no news.*
The previous version's deeper failure mode wasn't just hallucinating items —
it was also that any silent failure looked identical to a quiet day. Here they
are never conflated.

## Data sources — what, why authoritative, how to verify manually

### Section 1: IPO pipeline — SEC EDGAR daily master index
- **Endpoint:** `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/master.{YYYYMMDD}.idx`
- **What it is:** a pipe-delimited list of *every* filing EDGAR accepted that
  day (`CIK|Company|Form|Date|Filename`). Stable format since 1994. The file
  is finalized each evening, so the previous business day's file is complete
  by the time the 6:45am run fires. The script filters for S-1, S-1/A, F-1,
  F-1/A, and 424B4 and reports filer name, form, and date verbatim.
- **Why authoritative:** it *is* the registry. Nothing upstream of EDGAR exists.
- **Honesty refinement — "first-time filer" column:** an S-1 is not
  automatically an IPO. Already-public companies file S-1s for shelf and
  secondary offerings, and 424B4s for follow-ons. For each filer the script
  checks the SEC's official submissions API
  (`https://data.sec.gov/submissions/CIK##########.json`) for prior periodic
  reports (10-K/10-Q/8-K/20-F/40-F/6-K). No prior reports → first-time
  registrant → genuine IPO candidate, flagged **YES**. Any lookup failure
  prints `unknown`, never a guess.
- **Requirement:** the SEC's fair-access policy requires a declared
  User-Agent. Set the `SEC_CONTACT` secret to `"Your Name you@email.com"`.
  Undeclared bots get 403'd. Stay under 10 req/s (the script sleeps between
  submissions lookups).
- **Verify manually:** open the daily-index URL above for yesterday's date in
  a browser, Ctrl-F "424B4". Cross-check any filing at
  <https://efts.sec.gov/LATEST/search-index?q=%22initial+public+offering%22&forms=424B4>
  (the JSON API behind EDGAR full-text search — also key-free, also requires a UA).

### Section 2a: S&P DJI — PR Newswire (official distribution channel)
- **Endpoint:** `https://www.prnewswire.com/news/s%26p-dow-jones-indices/`
  (server-rendered HTML listing of every release with
  "SOURCE S&P Dow Jones Indices").
- **Why authoritative:** S&P DJI distributes its add/delete announcements
  *through* PR Newswire — the wire copy is the press release. The canonical
  archive is `press.spglobal.com` (linked from every release). Note: the main
  `spglobal.com/spdji` site sits behind Akamai bot protection and is hostile
  to datacenter IPs; `press.spglobal.com` is not — that's the fallback to use
  if PRN ever starts blocking GitHub's IP ranges.
- **What gets parsed:** release titles are filtered for "Set to Join" /
  "Set to Be Removed" / "will replace" / "Changes to the S&P" / rebalance
  language, then the structured table inside each release
  (Effective Date | Index | Action | Company | Ticker | GICS Sector) is
  extracted verbatim. If the table parse fails, the headline + date + link
  still go out — the headline itself usually carries the substance
  (e.g. "Marvell Technology and Flex Set to Join S&P 500").
- **Verify manually:** open the PRN listing above, or
  <https://press.spglobal.com/>, or the S&P DJI media center.

### Section 2b: Nasdaq — GlobeNewswire (official distribution channel)
- **Endpoint:** GlobeNewswire per-organization RSS for Nasdaq, Inc.
  (organization id **6948** — visible in the URL path of every Nasdaq release
  on globenewswire.com). The script tries the standard
  `/RssFeed/organization/6948/...` patterns and falls back to CHECK_FAILED
  with manual links if neither returns XML.
- **Why authoritative:** Nasdaq index announcements ("Nasdaq-100 Index®
  Quarterly/Annual Changes", replacement notices) are issued as GlobeNewswire
  releases, Source: Nasdaq, Inc. The body sentences naming additions,
  removals, and the effective date are extracted verbatim by regex; if the
  patterns don't match, headline + link only.
- **Important 2026 context:** Nasdaq updated the NDX methodology effective
  May 1, 2026 — quarterly rebalances now include constituent changes (the
  June 2026 quarterly rebalance, announced the evening of June 11, added and
  removed five names each). Expect *quarterly* NDX change announcements going
  forward, not just the December annual reconstitution.
- **Verify manually:** <https://indexes.nasdaqomx.com/> (Nasdaq Global Index
  Watch) or search "Nasdaq, Inc." on globenewswire.com.

### Section 2c: FTSE Russell — official calendar + best-effort notices
- **Layer 1 (cannot fail, cannot hallucinate):** the official 2026
  reconstitution calendar, hard-coded from FTSE Russell's own schedule press
  release (2026-03-02) — Rank Day Apr 30; preliminary lists May 22; updates
  May 29, Jun 5, Jun 12, Jun 18; effective after the close **Jun 26**;
  December semi-annual recon rank day Oct 30, effective Dec 11. On those
  dates the email flags the event and links the lists.
- **Layer 2 (fragile, fails honestly):** a scrape of
  <https://research.ftserussell.com/products/index-notices>. Individual
  notices (e.g. `.../home/getnotice/?id=NNNNNNN`) serve cleanly to scripts,
  but the *listing* is a JavaScript app, so discovery frequently degrades to
  CHECK_FAILED with manual links. This is the weakest source in the pipeline
  — by design it never silently reports "no updates" when it merely failed.
- **2026 structural change:** Russell reconstitution is now **semi-annual**
  (June + December), plus quarterly IPO additions (Mar/Jun/Sep/Dec). The
  hard-coded calendar must be refreshed each year when FTSE Russell publishes
  the new schedule (usually late winter) — this is the one piece of annual
  maintenance the pipeline needs.
- **Verify manually:** <https://www.ftserussell.com/resources/russell-reconstitution>
  (the membership lists) and the notices page above.

### Section 3: market snapshot — Yahoo Finance chart API (direct)
- **Endpoint:** `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=10d&interval=1d`
  with a browser User-Agent (per the known yfinance-on-Actions failure mode),
  falling back to `query2`. Symbols: `^GSPC`, `^IXIC`, `^DJI`, `^RUT`, `^TNX`.
- **Completed-session logic:** the script selects the last daily bar dated
  strictly before today (US/Eastern) that has both an open and a close —
  i.e. the last fully completed regular session. It never reads a partial or
  pre-market bar. Any symbol without clean data prints `N/A`.
- **Two conventions, both shown:**
  - **Session (O→C)** — that day's open to close, per the spec.
  - **vs prior close (C→C)** — the convention every headline uses. It
    includes the overnight gap (futures-driven moves that show up at the
    open), which the session measure deliberately excludes. When your number
    differs from CNBC's, this is why.
  `^TNX` quotes 10× the 10-year yield, so it's shown as a % level with
  changes in basis points.
- **Caveat:** this is the one *unofficial* source in the pipeline (Yahoo's
  endpoint is undocumented and occasionally rate-limits datacenter IPs). It's
  acceptable because a missing price degrades to `N/A` — it can never
  fabricate an IPO or index change. If it becomes flaky, Stooq's CSV endpoint
  (`stooq.com/q/d/l/?s=^spx&i=d`) is a drop-in fallback.

## Setup

1. Repo layout: `main.py`, `requirements.txt`,
   `.github/workflows/newsletter.yml` (move `newsletter.yml` there).
2. Repository secrets (Settings → Secrets and variables → Actions):
   - `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (existing)
   - `SEC_CONTACT` — `"Danny Le your@email.com"` (required by SEC policy)
   - `NEWSLETTER_TO` — optional, comma-separated; defaults to the sender.
3. Test locally or via Actions → Run workflow → dry_run = true:
   `python main.py --dry-run` builds `newsletter.html` without sending and
   logs a per-source SUMMARY line — read those five lines to audit any run.

## Reliable daily trigger (recommendation)

GitHub's `schedule` cron has two known problems: it is **silently disabled
after 60 days without repo activity**, and scheduled runs are queued
best-effort (delays of 15–60+ minutes at busy times are common).

**Recommended setup — external trigger, GitHub cron as backup:**

1. Create a fine-grained PAT with *Actions: read/write* on this repo.
2. On [cron-job.org](https://cron-job.org) (free, timezone-aware so DST is
   handled for you), create a job for 6:45 AM America/New_York, Mon–Fri:
   - URL: `https://api.github.com/repos/<you>/<repo>/dispatches`
   - Method: POST, body: `{"event_type": "send-newsletter"}`
   - Headers: `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`
3. Keep the two cron lines in the workflow as a backup. The guard step's
   once-per-day check means the backup never double-sends, and the dual-cron
   + ET-window check keeps the send time stable across DST.
4. The external POST also counts as activity, which sidesteps the 60-day
   auto-disable entirely.

## Expectations

"No index additions or deletions announced today" is the **correct** output
most days — outside the June/December Russell recons and the quarterly
S&P/NDX rebalance weeks, provider announcements happen only a handful of
times per quarter. EDGAR, by contrast, will show S-1/424B4 activity most
business days; the **First-time filer = YES** rows are the actual IPO signal.

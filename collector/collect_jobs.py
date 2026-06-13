#!/usr/bin/env python3
"""
Job Hunt Pilot — Collector v2
Scrapes fresh marketing/content roles (<= 1 week old) across LinkedIn, Indeed,
and Google using JobSpy, then filters by title match + experience level,
scores each job, and writes to Google Sheets.

Runs on GitHub Actions (free). No paid APIs required.
Optional: ScraperAPI free tier (1000 calls/month) for Naukri/LinkedIn access.

Env vars (set as GitHub Actions secrets):
  GOOGLE_SERVICE_ACCOUNT_JSON  -> full JSON of a Google service account key
  RAW_FEED_SHEET_ID            -> the Google Sheet ID to write into
  SCRAPER_API_KEY              -> (optional) ScraperAPI key for Naukri/LinkedIn
  PROXIES                      -> (optional) comma-separated fallback proxies
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timezone

import pandas as pd
from jobspy import scrape_jobs

# ─────────────────────────────────────────────────────────────────────────────
# SEARCH CONFIG (Kumar's target roles)
# ─────────────────────────────────────────────────────────────────────────────
TITLES = [
    "Content Strategist",
    "Content Marketing Strategist",
    "Content Marketing Manager",
    "Senior Copywriter",
    "SEO Content Specialist",
    "Head of Content",
    "Content Lead",
    "Associate Marketing Manager",
    "Growth Marketing Manager",
    "Growth Marketing Specialist",
    "Product Marketing Manager",
    "Brand Content Strategist",
]

LOCATIONS = ["Bengaluru, India", "India"]
HOURS_OLD = 168          # 1 week
RESULTS_PER_QUERY = 30
COUNTRY_INDEED = "India"

# Boards that work reliably from GitHub servers
BASE_SITES = ["linkedin", "indeed", "google"]

# Add Naukri/Glassdoor only if a ScraperAPI key is present
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "").strip()
if SCRAPER_API_KEY:
    BASE_SITES = ["linkedin", "indeed", "google", "naukri"]
    PROXIES = [f"http://scraperapi:{SCRAPER_API_KEY}@proxy-server.scraperapi.com:8001"]
    print("[info] ScraperAPI key found — adding Naukri, using proxy")
else:
    raw = os.getenv("PROXIES", "")
    PROXIES = [p.strip() for p in raw.split(",") if p.strip()] or None
    print("[info] No ScraperAPI key — using Indeed + LinkedIn + Google only")

MIN_SALARY_LPA = 20.0   # ₹20 LPA minimum (only applied when salary IS listed)
MIN_EXPERIENCE_YEARS = 3

# ─────────────────────────────────────────────────────────────────────────────
# TITLE MATCHING — The key fix for the 94% mismatch problem
# ─────────────────────────────────────────────────────────────────────────────
# A job passes if its ACTUAL TITLE contains at least one target keyword
# AND does not contain any hard-exclude keyword.
TITLE_MUST_CONTAIN = [
    "content strateg",      # content strategist, content strategy
    "content marketing",    # content marketing manager/strategist
    "content manager",
    "content lead",
    "head of content",
    "seo content",
    "copywriter",           # senior copywriter, lead copywriter
    "product marketing",    # product marketing manager
    "growth marketing",     # growth marketing manager/specialist
    "marketing manager",    # associate/senior marketing manager
    "brand strateg",        # brand strategist
    "content writer",       # borderline — keep for senior roles
]

TITLE_HARD_EXCLUDE = [
    "performance marketing",
    "digital marketing specialist",
    "social media manager",
    "social media strategist",
    "social media content",
    "data analyst",
    "software engineer",
    "developer",
    "sde ",
    "accountant",
    "finance",
    "hr ",
    " hr",
    "recruiter",
    "graphic design",
    "video editor",
    "animator",
    "fashion",
    "customer success",
    "sales executive",
    "business development",
    "telecaller",
    "executive - marketing",   # often telecalling disguised
]

# Low-experience patterns — drop if ANY found in description or experience_range
LOW_EXP_PATTERNS = [
    r"\b0[-–]1\s*year",
    r"\b0[-–]2\s*year",
    r"\b0[-–]3\s*year",
    r"\b0\s*[-–]\s*1\s*year",
    r"\b0\s*[-–]\s*2\s*year",
    r"\b0\s*[-–]\s*3\s*year",
    r"fresher",
    r"fresh graduate",
    r"entry.?level",
    r"no experience required",
    r"experience.*?:.*?0",
    r"experience.*?:.*?1 year",
]

# B2B/SaaS positive signals (boost score)
POSITIVE_SIGNALS = [
    "b2b", "saas", "content strategy", "seo", "storytelling",
    "brand voice", "editorial", "content calendar", "demand generation",
    "thought leadership", "product marketing", "go-to-market",
    "content ops", "content roadmap",
]

NEGATIVE_SIGNALS = [
    "fresher", "entry level", "performance marketing", "graphic design",
    "video production", "motion graphic", "telecall",
]


def is_title_match(title: str) -> bool:
    """True if the job's actual title is a genuine target role."""
    t = (title or "").lower()
    if not any(kw in t for kw in TITLE_MUST_CONTAIN):
        return False
    if any(ex in t for ex in TITLE_HARD_EXCLUDE):
        return False
    return True


def has_min_experience(description: str, experience_range: str) -> bool:
    """False if the posting explicitly requires < MIN_EXPERIENCE_YEARS."""
    text = ((description or "") + " " + (experience_range or "")).lower()
    for pat in LOW_EXP_PATTERNS:
        if re.search(pat, text):
            return False
    return True


def salary_passes(min_amt, max_amt, currency) -> bool:
    """True if salary is unknown (keep) or >= MIN_SALARY_LPA (keep)."""
    if not min_amt and not min_amt != 0:
        return True   # no data → keep
    try:
        amt = float(str(min_amt).replace(",", ""))
        if not amt:
            return True
        # JobSpy returns annual INR in lakhs for Indian jobs when currency=INR
        # If it's in rupees (large numbers), convert to LPA
        if amt > 1000:
            amt = amt / 100000  # convert paise/rupees to LPA
        return amt >= MIN_SALARY_LPA
    except Exception:
        return True   # parse failure → keep


def compute_pre_score(job_title: str, description: str, search_title: str) -> int:
    """
    Basic 0-100 pre-score so the skill can immediately see quality.
    Full 0-100 scoring happens inside Claude during the daily pilot run.
    """
    score = 0
    title_l = (job_title or "").lower()
    search_l = (search_title or "").lower()
    desc_l   = (description or "").lower()

    # Title match quality (up to 40 pts)
    if search_l in title_l:
        score += 40                    # exact match
    elif any(w in title_l for w in search_l.split() if len(w) > 4):
        score += 25                    # partial match

    # Positive content signals in description (up to 30 pts)
    hits = sum(1 for s in POSITIVE_SIGNALS if s in desc_l)
    score += min(30, hits * 5)

    # Negative signals (penalty)
    penalties = sum(1 for s in NEGATIVE_SIGNALS if s in desc_l)
    score -= penalties * 8

    # Base offset so nothing starts at 0
    score += 15

    return max(0, min(100, score))


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────────────────────────────────────
def scrape_all() -> pd.DataFrame:
    frames = []
    for title in TITLES:
        for loc in LOCATIONS:
            google_q = f"{title} jobs {loc} past week"
            try:
                df = scrape_jobs(
                    site_name=BASE_SITES,
                    search_term=title,
                    google_search_term=google_q,
                    location=loc,
                    results_wanted=RESULTS_PER_QUERY,
                    hours_old=HOURS_OLD,
                    country_indeed=COUNTRY_INDEED,
                    linkedin_fetch_description=True,
                    proxies=PROXIES,
                )
                if df is not None and len(df):
                    df["search_title"] = title
                    df["search_location"] = loc
                    frames.append(df)
                    print(f"[ok] '{title}' @ {loc}: {len(df)} raw rows")
                else:
                    print(f"[warn] '{title}' @ {loc}: 0 rows returned")
            except Exception as e:
                print(f"[warn] '{title}' @ {loc}: {type(e).__name__}: {str(e)[:160]}")
            time.sleep(2)

    if not frames:
        return pd.DataFrame()

    allj = pd.concat(frames, ignore_index=True)
    raw_count = len(allj)

    # ── De-dupe ──────────────────────────────────────────────────────────────
    allj = allj.drop_duplicates(subset=["job_url"]).copy()
    allj = allj.drop_duplicates(subset=["company", "title"]).copy()
    print(f"[info] After de-dupe: {len(allj)} (was {raw_count})")

    # ── FILTER 1: Title must be a genuine target role ─────────────────────────
    before = len(allj)
    allj = allj[allj["title"].apply(is_title_match)].copy()
    print(f"[filter] Title match: kept {len(allj)} / {before}")

    # ── FILTER 2: Experience >= 3 years ───────────────────────────────────────
    before = len(allj)
    allj = allj[allj.apply(
        lambda r: has_min_experience(r.get("description", ""), r.get("experience_range", "")),
        axis=1
    )].copy()
    print(f"[filter] Experience ≥3yr: kept {len(allj)} / {before}")

    # ── FILTER 3: Salary floor (only when listed) ─────────────────────────────
    before = len(allj)
    allj = allj[allj.apply(
        lambda r: salary_passes(r.get("min_amount"), r.get("max_amount"), r.get("currency")),
        axis=1
    )].copy()
    print(f"[filter] Salary floor ≥₹20LPA: kept {len(allj)} / {before}")

    # ── Pre-score ─────────────────────────────────────────────────────────────
    allj["pre_score"] = allj.apply(
        lambda r: compute_pre_score(r.get("title", ""), r.get("description", ""), r.get("search_title", "")),
        axis=1
    )

    # ── Sort by pre_score descending ──────────────────────────────────────────
    allj = allj.sort_values("pre_score", ascending=False)

    allj["collected_at"] = datetime.now(timezone.utc).isoformat()
    return allj


# ─────────────────────────────────────────────────────────────────────────────
# WRITE TO GOOGLE SHEETS
# ─────────────────────────────────────────────────────────────────────────────
SHEET_COLUMNS = [
    "pre_score", "collected_at", "search_title", "search_location",
    "site", "title", "company", "location", "is_remote", "job_type",
    "date_posted", "min_amount", "max_amount", "currency",
    "job_url", "description", "skills", "experience_range", "company_rating",
]

def write_to_sheet(df: pd.DataFrame):
    sheet_id = os.getenv("RAW_FEED_SHEET_ID")
    sa_json  = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not (sheet_id and sa_json):
        out = "job_raw_feed.csv"
        df.to_csv(out, index=False)
        print(f"[fallback] No Sheets creds; wrote {len(df)} rows to {out}")
        return

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    ws = sh.sheet1

    for c in SHEET_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df = df[SHEET_COLUMNS].fillna("")

    ws.clear()
    ws.update([df.columns.tolist()] + df.astype(str).values.tolist())
    print(f"[ok] Wrote {len(df)} rows to Google Sheet {sheet_id}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("=== Job Hunt Pilot collector v2 ===")
    df = scrape_all()
    if df.empty:
        print("[done] 0 qualifying jobs this run. Skill will fall back to web_search.")
        write_to_sheet(pd.DataFrame(columns=SHEET_COLUMNS))
        sys.exit(0)
    print(f"[done] {len(df)} qualifying jobs after all filters")
    write_to_sheet(df)


if __name__ == "__main__":
    main()

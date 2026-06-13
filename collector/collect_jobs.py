#!/usr/bin/env python3
"""
Job Hunt Pilot — Collector
Scrapes fresh marketing/content roles (<= 1 week old) across LinkedIn, Naukri,
Indeed, Google and Glassdoor using JobSpy, de-dupes, and writes to a Google
Sheet named 'Job_Raw_Feed' that the Claude skill reads later.

Runs on GitHub Actions (free) or locally. No paid APIs.

Env vars expected (set as GitHub Actions secrets):
  GOOGLE_SERVICE_ACCOUNT_JSON  -> full JSON of a Google service account key
  RAW_FEED_SHEET_ID            -> the Google Sheet ID to write into
Optional:
  PROXIES                      -> comma-separated proxy list to ease LinkedIn throttling
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

import pandas as pd
from jobspy import scrape_jobs

# ---- Search configuration (Kumar's profile) -------------------------------
TITLES = [
    "Content Strategist",
    "Content Marketing Strategist",
    "Content Marketing Manager",
    "Senior Copywriter",
    "SEO Content Specialist",
    "Head of Content",
    "Associate Marketing Manager",
    "Growth Marketing Specialist",
    "Growth Marketing Manager",
    "Product Marketing Manager",
    "Brand Strategist",
    "Content Lead",
]
LOCATIONS = ["Bengaluru, India", "India"]   # 'India' net catches Remote-India
HOURS_OLD = 168                              # one week
RESULTS_PER_QUERY = 25
SITES = ["linkedin", "naukri", "indeed", "google", "glassdoor"]
COUNTRY_INDEED = "India"

PROXIES = [p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()] or None


def scrape_all() -> pd.DataFrame:
    frames = []
    for title in TITLES:
        for loc in LOCATIONS:
            google_q = f"{title} jobs near {loc} since last week"
            try:
                df = scrape_jobs(
                    site_name=SITES,
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
                print(f"[ok] {title} @ {loc}: {0 if df is None else len(df)} rows")
            except Exception as e:
                # Never let one throttled board kill the whole run
                print(f"[warn] {title} @ {loc}: {type(e).__name__}: {str(e)[:140]}")
            time.sleep(2)  # be polite, reduce throttling
    if not frames:
        return pd.DataFrame()
    allj = pd.concat(frames, ignore_index=True)
    # De-dupe: same job_url, or same company+title pair
    allj = allj.drop_duplicates(subset=["job_url"]).copy()
    allj = allj.drop_duplicates(subset=["company", "title"]).copy()
    allj["collected_at"] = datetime.now(timezone.utc).isoformat()
    return allj


def write_to_sheet(df: pd.DataFrame):
    """Write to Google Sheet. Falls back to CSV if Sheets creds absent."""
    sheet_id = os.getenv("RAW_FEED_SHEET_ID")
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

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

    # Keep a stable, skill-friendly column order
    cols = [
        "collected_at", "search_title", "search_location", "site", "title",
        "company", "location", "is_remote", "job_type", "date_posted",
        "min_amount", "max_amount", "currency", "job_url", "description",
        "skills", "experience_range", "company_rating",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols].fillna("")

    # Overwrite the feed each run (it's a rolling 1-week window; the tracker is the memory)
    ws.clear()
    ws.update([df.columns.tolist()] + df.astype(str).values.tolist())
    print(f"[ok] Wrote {len(df)} rows to Google Sheet {sheet_id}")


def main():
    print("=== Job Hunt Pilot collector ===")
    df = scrape_all()
    if df.empty:
        print("[done] 0 jobs this run (boards may be throttling). "
              "Skill will fall back to web_search.")
        # Still write an empty sheet so the skill sees a fresh timestamp
        write_to_sheet(pd.DataFrame(columns=["collected_at"]))
        sys.exit(0)
    print(f"[done] {len(df)} unique jobs after de-dupe")
    write_to_sheet(df)


if __name__ == "__main__":
    main()

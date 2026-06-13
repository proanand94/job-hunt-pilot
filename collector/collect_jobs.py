#!/usr/bin/env python3
"""
Job Hunt Pilot — Collector v4
- Platforms: LinkedIn + Indeed + Google Jobs + Cutshort (+ Naukri via ScraperAPI)
- Cross-day deduplication via 'seen_urls' tab in the workbook
- Archives each day's new jobs to a dated tab (e.g. "2026-06-14")
- Job_Raw_Feed (Sheet1) always shows TODAY's new jobs only
- Writes email_body.html for the email notification step

Secrets required in GitHub Actions:
  GOOGLE_SERVICE_ACCOUNT_JSON  -> Google service account JSON key
  RAW_FEED_SHEET_ID            -> Google Sheet ID
  GMAIL_USERNAME               -> your Gmail address (for email notification)
  GMAIL_APP_PASSWORD           -> Gmail App Password (16-char, from Google Account > Security)
  SCRAPER_API_KEY              -> (optional) ScraperAPI key for Naukri
"""

import os, re, sys, json, time, requests
from datetime import datetime, timezone
import pandas as pd
from jobspy import scrape_jobs

# ─── Target roles ─────────────────────────────────────────────────────────────
TITLES = [
    "Content Strategist", "Content Marketing Strategist",
    "Content Marketing Manager", "Senior Copywriter",
    "SEO Content Specialist", "Head of Content", "Content Lead",
    "Associate Marketing Manager", "Growth Marketing Manager",
    "Growth Marketing Specialist", "Product Marketing Manager",
    "Brand Content Strategist",
]
LOCATIONS   = ["Bengaluru, India", "India"]
HOURS_OLD   = 168
RESULTS_PER = 30
COUNTRY     = "India"
TODAY       = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ─── Proxy / ScraperAPI ───────────────────────────────────────────────────────
SCRAPER_KEY = os.getenv("SCRAPER_API_KEY", "").strip()
if SCRAPER_KEY:
    BASE_SITES = ["linkedin", "indeed", "google", "naukri"]
    PROXIES    = [f"http://scraperapi:{SCRAPER_KEY}@proxy-server.scraperapi.com:8001"]
    print("[info] ScraperAPI active — LinkedIn + Indeed + Google + Naukri")
else:
    BASE_SITES = ["linkedin", "indeed", "google"]
    raw = os.getenv("PROXIES", "")
    PROXIES = [p.strip() for p in raw.split(",") if p.strip()] or None
    print("[info] No ScraperAPI key — LinkedIn + Indeed + Google only")

# ─── Filters ──────────────────────────────────────────────────────────────────
MIN_SALARY_LPA = 20.0

TITLE_MUST_CONTAIN = [
    "content strateg", "content marketing", "content manager", "content lead",
    "head of content", "seo content", "copywriter", "product marketing",
    "growth marketing", "marketing manager", "brand strateg", "content writer",
]
TITLE_HARD_EXCLUDE = [
    "performance marketing", "digital marketing specialist", "social media manager",
    "social media strategist", "social media content", "data analyst",
    "software engineer", "developer", "sde ", "accountant", "finance",
    "hr ", " hr", "recruiter", "graphic design", "video editor", "animator",
    "customer success", "sales executive", "business development", "telecaller",
    "executive - marketing",
]
LOW_EXP_PATTERNS = [
    r"\b0[-–][123]\s*year", r"\b0\s*[-–]\s*[123]\s*year",
    r"fresher", r"fresh graduate", r"entry.?level",
    r"no experience required", r"experience.*?:.*?[01]\s*year",
]
POSITIVE_SIGNALS = [
    "b2b", "saas", "content strategy", "seo", "storytelling", "brand voice",
    "editorial", "content calendar", "demand generation", "thought leadership",
    "product marketing", "go-to-market", "content ops", "content roadmap",
]
NEGATIVE_SIGNALS = [
    "fresher", "entry level", "performance marketing", "graphic design",
    "video production", "motion graphic", "telecall",
]

def is_title_match(title):
    t = (title or "").lower()
    return (any(k in t for k in TITLE_MUST_CONTAIN) and
            not any(x in t for x in TITLE_HARD_EXCLUDE))

def has_min_experience(desc, exp_range):
    text = ((desc or "") + " " + (exp_range or "")).lower()
    return not any(re.search(p, text) for p in LOW_EXP_PATTERNS)

def salary_ok(min_amt, *_):
    if not min_amt: return True
    try:
        amt = float(str(min_amt).replace(",", ""))
        if amt > 1000: amt /= 100000
        return not amt or amt >= MIN_SALARY_LPA
    except Exception:
        return True

def pre_score(job_title, desc, search_title):
    tl, sl, dl = (job_title or "").lower(), (search_title or "").lower(), (desc or "").lower()
    s  = 40 if sl in tl else (25 if any(w in tl for w in sl.split() if len(w) > 4) else 0)
    s += min(30, sum(5 for p in POSITIVE_SIGNALS if p in dl))
    s -= sum(8  for n in NEGATIVE_SIGNALS if n in dl)
    return max(0, min(100, s + 15))

def fix_site_label(row):
    site = row.get("site")
    if site and str(site).strip() not in ("", "None", "nan"):
        return str(site).strip()
    url = str(row.get("job_url") or "")
    if "linkedin.com"  in url: return "linkedin"
    if "indeed.com"    in url: return "indeed"
    if "naukri.com"    in url: return "naukri"
    return "google"

# ─── Cutshort scraper ─────────────────────────────────────────────────────────
CUTSHORT_KEYWORDS = [
    "content strategist", "content marketing manager",
    "product marketing manager", "growth marketing manager",
    "head of content", "senior copywriter",
]

def scrape_cutshort() -> pd.DataFrame:
    rows, headers = [], {
        "User-Agent": "Mozilla/5.0 (compatible; JobHuntBot/1.0)",
        "Accept": "application/json",
        "Referer": "https://cutshort.io/",
    }
    for kw in CUTSHORT_KEYWORDS:
        try:
            r = requests.get(
                "https://cutshort.io/api/jobs/search",
                params={"q": kw, "location": "Bangalore", "limit": 20, "offset": 0},
                headers=headers, timeout=15,
            )
            if r.status_code != 200:
                print(f"[cutshort] {kw}: HTTP {r.status_code}"); continue
            for j in r.json().get("data", r.json().get("jobs", [])):
                slug = j.get("slug") or j.get("id", "")
                rows.append({
                    "site": "cutshort", "search_title": kw, "search_location": "Bengaluru, India",
                    "title": j.get("title",""), "company": (j.get("company") or {}).get("name",""),
                    "location": j.get("location","Bangalore"),
                    "is_remote": j.get("isRemote", False), "job_type": "fulltime",
                    "date_posted": (j.get("createdAt","") or "")[:10],
                    "job_url": f"https://cutshort.io/job/{slug}" if slug else "",
                    "description": j.get("description",""),
                    "min_amount": "", "max_amount": "", "currency": "INR",
                    "skills": "", "experience_range": "", "company_rating": "",
                })
            print(f"[cutshort] '{kw}': {len(rows)} rows so far")
        except Exception as e:
            print(f"[cutshort] '{kw}': {type(e).__name__}: {str(e)[:120]}")
        time.sleep(1)
    return pd.DataFrame(rows) if rows else pd.DataFrame()

# ─── Scrape all platforms ─────────────────────────────────────────────────────
def scrape_all() -> pd.DataFrame:
    frames = []
    for title in TITLES:
        for loc in LOCATIONS:
            try:
                df = scrape_jobs(
                    site_name=BASE_SITES, search_term=title,
                    google_search_term=f"{title} jobs {loc} past week",
                    location=loc, results_wanted=RESULTS_PER, hours_old=HOURS_OLD,
                    country_indeed=COUNTRY, linkedin_fetch_description=True, proxies=PROXIES,
                )
                if df is not None and len(df):
                    df["search_title"] = title
                    df["search_location"] = loc
                    frames.append(df)
                    print(f"[ok] '{title}' @ {loc}: {len(df)} raw rows")
                else:
                    print(f"[warn] '{title}' @ {loc}: 0 rows")
            except Exception as e:
                print(f"[warn] '{title}' @ {loc}: {type(e).__name__}: {str(e)[:160]}")
            time.sleep(2)

    cs = scrape_cutshort()
    if not cs.empty:
        frames.append(cs)

    if not frames:
        return pd.DataFrame()

    allj = pd.concat(frames, ignore_index=True)
    raw  = len(allj)
    allj["site"] = allj.apply(fix_site_label, axis=1)
    allj = allj.drop_duplicates(subset=["job_url"]).copy()
    allj = allj.drop_duplicates(subset=["company", "title"]).copy()
    print(f"[info] After de-dupe: {len(allj)} (was {raw})")

    b = len(allj); allj = allj[allj["title"].apply(is_title_match)].copy()
    print(f"[filter] Title match: kept {len(allj)} / {b}")

    b = len(allj)
    allj = allj[allj.apply(lambda r: has_min_experience(
        r.get("description",""), r.get("experience_range","")), axis=1)].copy()
    print(f"[filter] Experience ≥3yr: kept {len(allj)} / {b}")

    b = len(allj)
    allj = allj[allj.apply(lambda r: salary_ok(
        r.get("min_amount"), r.get("max_amount"), r.get("currency")), axis=1)].copy()
    print(f"[filter] Salary ≥₹20LPA: kept {len(allj)} / {b}")

    allj["pre_score"] = allj.apply(lambda r: pre_score(
        r.get("title",""), r.get("description",""), r.get("search_title","")), axis=1)
    allj = allj.sort_values("pre_score", ascending=False)
    allj["collected_at"] = datetime.now(timezone.utc).isoformat()
    return allj

# ─── Google Sheets helpers ────────────────────────────────────────────────────
SHEET_COLS = [
    "pre_score", "collected_at", "search_title", "search_location",
    "site", "title", "company", "location", "is_remote", "job_type",
    "date_posted", "min_amount", "max_amount", "currency",
    "job_url", "description", "skills", "experience_range", "company_rating",
]

def get_seen_urls(sh) -> set:
    """Read all job URLs ever collected from the 'seen_urls' tab."""
    try:
        import gspread
        ws = sh.worksheet("seen_urls")
        vals = ws.col_values(1)
        return set(v.strip() for v in vals if v.strip() and v != "job_url")
    except Exception:
        # Tab doesn't exist yet — create it
        ws = sh.add_worksheet(title="seen_urls", rows=5000, cols=2)
        ws.update("A1:B1", [["job_url", "date_seen"]])
        print("[info] Created 'seen_urls' tab")
        return set()

def append_seen_urls(sh, urls: list):
    """Append today's new URLs to the seen_urls tab."""
    if not urls: return
    ws = sh.worksheet("seen_urls")
    ws.append_rows([[u, TODAY] for u in urls], value_input_option="RAW")
    print(f"[seen_urls] Appended {len(urls)} new URLs")

def write_dated_tab(sh, df: pd.DataFrame):
    """Create/replace a tab named by today's date for archival."""
    try:
        import gspread
        try:
            ws = sh.worksheet(TODAY)
            ws.clear()
        except Exception:
            ws = sh.add_worksheet(title=TODAY, rows=len(df) + 10, cols=len(SHEET_COLS) + 2)
        ws.update([df.columns.tolist()] + df.astype(str).values.tolist())
        print(f"[archive] Wrote {len(df)} rows to tab '{TODAY}'")
    except Exception as e:
        print(f"[archive] Could not write dated tab: {e}")

def write_main_sheet(sh, df: pd.DataFrame):
    """Overwrite Sheet1 (Job_Raw_Feed) with today's new jobs."""
    ws = sh.sheet1
    ws.clear()
    ws.update([df.columns.tolist()] + df.astype(str).values.tolist())
    print(f"[ok] Job_Raw_Feed updated with {len(df)} new jobs")

# ─── Email body ───────────────────────────────────────────────────────────────
def write_email_body(df: pd.DataFrame, new_count: int):
    """Generate an HTML email summary and save to email_body.html."""
    top = df.head(10)
    rows_html = ""
    for _, r in top.iterrows():
        score = r.get("pre_score", "—")
        title = r.get("title", "—")
        company = r.get("company", "—")
        loc = r.get("location", "—")
        url = r.get("job_url", "#")
        date_p = r.get("date_posted", "—")
        site = r.get("site", "—")
        rows_html += f"""
        <tr>
          <td style="padding:6px 10px;font-weight:bold;color:#1a73e8">{score}%</td>
          <td style="padding:6px 10px"><a href="{url}" style="color:#1a73e8;text-decoration:none">{title}</a></td>
          <td style="padding:6px 10px">{company}</td>
          <td style="padding:6px 10px">{loc}</td>
          <td style="padding:6px 10px;font-size:12px;color:#666">{site} · {date_p}</td>
        </tr>"""

    sheet_id = os.getenv("RAW_FEED_SHEET_ID", "")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else "#"

    html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px">
  <h2 style="color:#1a73e8">🎯 Job Hunt — {new_count} new jobs found today ({TODAY})</h2>
  <p style="color:#555">Here are the top matches from today's run. Open the full list in your 
  <a href="{sheet_url}">Google Sheet</a>.</p>

  <table style="border-collapse:collapse;width:100%">
    <thead>
      <tr style="background:#f0f4ff">
        <th style="padding:8px 10px;text-align:left">Score</th>
        <th style="padding:8px 10px;text-align:left">Role</th>
        <th style="padding:8px 10px;text-align:left">Company</th>
        <th style="padding:8px 10px;text-align:left">Location</th>
        <th style="padding:8px 10px;text-align:left">Source</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>

  <p style="margin-top:20px;color:#555">
    Open Claude and say <strong>"run my job hunt"</strong> to score these, approve the best ones,
    and generate tailored CVs.
  </p>
  <p style="font-size:12px;color:#999">Job Hunt Pilot · automated by GitHub Actions</p>
</body></html>"""

    with open("email_body.html", "w") as f:
        f.write(html)
    print(f"[email] email_body.html written ({new_count} jobs, top {len(top)} shown)")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print(f"=== Job Hunt Pilot collector v4 — {TODAY} ===")

    sheet_id = os.getenv("RAW_FEED_SHEET_ID")
    sa_json  = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    # Scrape
    df = scrape_all()

    if df.empty:
        print("[done] 0 qualifying jobs scraped. No email will be sent.")
        sys.exit(0)

    # Connect to Sheets (needed for dedup and writing)
    if sheet_id and sa_json:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(sa_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)

        # Cross-day deduplication
        seen = get_seen_urls(sh)
        before = len(df)
        df = df[~df["job_url"].isin(seen)].copy()
        print(f"[dedup] Removed {before - len(df)} already-seen jobs → {len(df)} truly new")

        if df.empty:
            print("[done] All jobs already seen in previous runs. Nothing new today.")
            sys.exit(0)

        # Prepare final columns
        for c in SHEET_COLS:
            if c not in df.columns:
                df[c] = ""
        df = df[SHEET_COLS].fillna("")

        # Write to Sheet1 (today's new jobs)
        write_main_sheet(sh, df)

        # Archive to dated tab
        write_dated_tab(sh, df)

        # Update seen_urls
        append_seen_urls(sh, df["job_url"].dropna().tolist())

    else:
        # Fallback: no Sheets creds, write CSV
        for c in SHEET_COLS:
            if c not in df.columns:
                df[c] = ""
        df = df[SHEET_COLS].fillna("")
        df.to_csv("job_raw_feed.csv", index=False)
        print(f"[fallback] No Sheets creds — wrote {len(df)} rows to job_raw_feed.csv")

    print(f"[done] {len(df)} new qualifying jobs written")

    # Write email body (GitHub Actions will send it)
    write_email_body(df, len(df))

if __name__ == "__main__":
    main()

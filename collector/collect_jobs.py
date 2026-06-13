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

# Add Naukri only if a ScraperAPI key is present
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "").strip()
if SCRAPER_API_KEY:
    BASE_SITES = ["linkedin", "indeed", "google", "naukri"]
    PROXIES = [f"http://scraperapi:{SCRAPER_API_KEY}@proxy-server.scraperapi.com:8001"]
    print("[info] ScraperAPI key found — adding Naukri, using proxy")
else:
    raw = os.getenv("PROXIES", "")
    PROXIES = [p.strip() for p in raw.split(",") if p.strip()] or None
    print("[info] No ScraperAPI key — using Indeed + LinkedIn + Google only")

MIN_SALARY_LPA = 20.0
MIN_EXPERIENCE_YEARS = 3

# ─────────────────────────────────────────────────────────────────────────────
# TITLE MATCHING
# ─────────────────────────────────────────────────────────────────────────────
TITLE_MUST_CONTAIN = [
    "content strateg",
    "content marketing",
    "content manager",
    "content lead",
    "head of content",
    "seo content",
    "copywriter",
    "product marketing",
    "growth marketing",
    "marketing manager",
    "brand strateg",
    "content writer",
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
    "executive - marketing",
]

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
    t = (title or "").lower()
    if not any(kw in t for kw in TITLE_MUST_CONTAIN):
        return False
    if any(ex in t for ex in TITLE_HARD_EXCLUDE):
        return False
    return True


def has_min_experience(description: str, experience_range: str) ->

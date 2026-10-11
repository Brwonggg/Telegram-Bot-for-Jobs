"""Job fetchers (MyCareersFuture, JSearch, Adzuna) plus the recency filter and dedupe.

Every job carries a UTC `posted_at`. `filter_recent` is the single place the
"posted within N hours" rule is enforced — jobs with no known date are dropped.
"""
import html
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)
TIMEOUT = 20


@dataclass
class Job:
    source: str
    title: str
    company: str
    location: str
    url: str
    posted_at: datetime | None  # timezone-aware UTC
    description: str = ""
    salary: str = ""

    @property
    def key(self):
        """Same job from different sources/reposts maps to the same key."""
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
        return f"{norm(self.title)}|{norm(self.company)}"


def _clean(text, limit=1500):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit]


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _salary(lo, hi, suffix=""):
    if not lo and not hi:
        return ""
    if lo and hi and lo != hi:
        return f"${lo:,.0f}–${hi:,.0f}{suffix}"
    return f"${(lo or hi):,.0f}{suffix}"


def fetch_mycareersfuture(keyword, max_age_hours=24):
    r = requests.get(
        "https://api.mycareersfuture.gov.sg/v2/jobs",
        params={"search": keyword, "limit": 50, "page": 0, "sortBy": "new_posting_date"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    jobs = []
    for j in r.json().get("results", []):
        meta = j.get("metadata") or {}
        company = (j.get("hiringCompany") or {}).get("name") or (j.get("postedCompany") or {}).get("name", "")
        sal = j.get("salary") or {}
        districts = (j.get("address") or {}).get("districts") or []
        jobs.append(Job(
            source="MyCareersFuture",
            title=j.get("title", ""),
            company=company,
            location=districts[0].get("location", "Singapore") if districts else "Singapore",
            url=meta.get("jobDetailsUrl", ""),
            # createdAt is the exact first-posted time; reposts keep their original date.
            posted_at=parse_iso(meta.get("createdAt")),
            description=_clean(j.get("description")),
            salary=_salary(sal.get("minimum"), sal.get("maximum"), "/mth"),
        ))
    return jobs


_jsearch_calls = {}  # {date: count} — keeps us inside the free tier (~200 requests/month)


def fetch_jsearch(keyword, max_age_hours=24):
    key = os.environ.get("RAPIDAPI_KEY")
    today = datetime.now(timezone.utc).date()
    if not key or _jsearch_calls.get(today, 0) >= int(os.environ.get("JSEARCH_DAILY_LIMIT", 6)):
        return []
    _jsearch_calls[today] = _jsearch_calls.get(today, 0) + 1
    date_posted = "today" if max_age_hours <= 24 else "3days" if max_age_hours <= 72 else "week" if max_age_hours <= 168 else "month"
    r = requests.get(
        "https://jsearch.p.rapidapi.com/search",
        params={"query": f"{keyword} in Singapore", "country": "sg", "date_posted": date_posted, "num_pages": 1},
        headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    jobs = []
    for j in r.json().get("data", []):
        ts = j.get("job_posted_at_timestamp")
        posted = datetime.fromtimestamp(ts, timezone.utc) if ts else parse_iso(j.get("job_posted_at_datetime_utc"))
        jobs.append(Job(
            source="JSearch",
            title=j.get("job_title", ""),
            company=j.get("employer_name", ""),
            location=j.get("job_city") or j.get("job_country") or "",
            url=j.get("job_apply_link", ""),
            posted_at=posted,
            description=_clean(j.get("job_description")),
            salary=_salary(j.get("job_min_salary"), j.get("job_max_salary")),
        ))
    return jobs


def fetch_adzuna(keyword, max_age_hours=24):
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    r = requests.get(
        "https://api.adzuna.com/v1/api/jobs/sg/search/1",
        params={
            "app_id": app_id, "app_key": app_key, "what": keyword, "results_per_page": 50,
            "max_days_old": max(1, math.ceil(max_age_hours / 24)), "sort_by": "date",
            "content-type": "application/json",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return [
        Job(
            source="Adzuna",
            title=j.get("title", ""),
            company=(j.get("company") or {}).get("display_name", ""),
            location=(j.get("location") or {}).get("display_name", ""),
            url=j.get("redirect_url", ""),
            posted_at=parse_iso(j.get("created")),
            description=_clean(j.get("description")),
            salary=_salary(j.get("salary_min"), j.get("salary_max")),
        )
        for j in r.json().get("results", [])
    ]


FETCHERS = [fetch_mycareersfuture, fetch_jsearch, fetch_adzuna]


def filter_recent(jobs, max_age_hours=24, now=None):
    """Keep only jobs with a known posting time within the last `max_age_hours`."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    return [j for j in jobs if j.posted_at is not None and cutoff <= j.posted_at <= now + timedelta(minutes=5)]


def dedupe(jobs):
    seen, out = set(), []
    for j in jobs:
        if j.key not in seen and j.url:
            seen.add(j.key)
            out.append(j)
    return out


def search_jobs(keywords, max_age_hours=24):
    """Query every source for every keyword; return recent, de-duplicated jobs, newest first."""
    jobs = []
    for kw in keywords:
        for fetch in FETCHERS:
            try:
                jobs.extend(fetch(kw, max_age_hours))
            except Exception as e:
                log.warning("%s failed for %r: %s", fetch.__name__, kw, e)
    jobs = dedupe(filter_recent(jobs, max_age_hours))
    return sorted(jobs, key=lambda j: j.posted_at, reverse=True)

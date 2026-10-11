import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from job_sources import Job, dedupe, filter_recent

NOW = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)


def job(title="Data Scientist", company="Acme", hours_ago=2.0, url="https://x"):
    posted = None if hours_ago is None else NOW - timedelta(hours=hours_ago)
    return Job("test", title, company, "SG", url, posted)


def test_keeps_recent_job():
    assert len(filter_recent([job(hours_ago=2)], now=NOW)) == 1


def test_drops_job_older_than_24h():
    assert filter_recent([job(hours_ago=25)], now=NOW) == []


def test_drops_job_without_date():
    assert filter_recent([job(hours_ago=None)], now=NOW) == []


def test_custom_window():
    assert len(filter_recent([job(hours_ago=60)], max_age_hours=72, now=NOW)) == 1


def test_dedupe_same_title_company_across_sources():
    a = job(title="Data Scientist", company="Acme Pte. Ltd.")
    b = job(title="data scientist", company="ACME Pte Ltd")
    assert len(dedupe([a, b])) == 1


def test_dedupe_drops_jobs_without_url():
    assert dedupe([job(url="")]) == []

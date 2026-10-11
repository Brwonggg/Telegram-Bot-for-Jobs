"""Daily digest: jobs posted in the last 24h that match the profile, summarised by the AI model."""
import html
import logging

import assistant
import db
from job_sources import dedupe, filter_recent, search_jobs

log = logging.getLogger(__name__)


def build_digest(chat_id):
    """Return [(job, score, summary)] of new, relevant jobs posted in the last 24 hours."""
    profile = (db.get_user(chat_id) or {}).get("profile") or {}
    keywords = profile.get("search_keywords") or profile.get("target_roles") or []
    jobs = search_jobs(keywords, max_age_hours=24)
    jobs += filter_recent(assistant.web_job_search(profile), max_age_hours=24)
    sent = db.sent_job_keys(chat_id)
    jobs = [j for j in dedupe(jobs) if j.key not in sent]
    log.info("digest for %s: %d new candidate jobs", chat_id, len(jobs))
    picked = assistant.rank_and_summarize(profile, jobs)
    db.mark_jobs_sent(chat_id, [job.key for job, _, _ in picked])
    return picked


def format_job(job, summary):
    """Telegram HTML message for one job."""
    hours = assistant.hours_ago(job.posted_at)
    meta = " · ".join(x for x in [job.location, job.salary, f"posted {hours}h ago", job.source] if x)
    e = html.escape
    return f"<b>{e(job.title)}</b> — {e(job.company)}\n<i>{e(meta)}</i>\n\n{e(summary)}"

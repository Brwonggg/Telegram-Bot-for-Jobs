"""All AI calls (OpenAI Responses API): chat with job-search tools, profile building, job ranking/summaries, web job search."""
import base64
import json
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import openai

import db
from job_sources import Job, parse_iso, search_jobs

log = logging.getLogger(__name__)

MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
SGT = ZoneInfo("Asia/Singapore")

_client = None


def client():
    global _client
    if _client is None:
        _client = openai.OpenAI()
    return _client


SYSTEM_PROMPT = """You are a personal job-search assistant on Telegram for an undergraduate studying \
Data Science & Artificial Intelligence (DSAI) at Nanyang Technological University (NTU), Singapore.

Help with anything job-search related: finding openings (internships, graduate roles, part-time), \
judging fit, resume and cover-letter feedback, interview prep, salary expectations in Singapore, \
company research, application strategy and timelines. You can also chat about anything else like a normal assistant.

Tools:
- search_jobs: live listings from MyCareersFuture, JSearch (LinkedIn/Indeed/Glassdoor/Google Jobs) and Adzuna, \
filtered by posting time. Prefer this for "find me jobs" requests.
- web_search: anything else on the web (company pages, career portals, salary data, interview \
experiences, application deadlines).
- update_profile: when the user states a lasting change to their preferences (new target role, location, \
job type, things to avoid), save it so their daily digest follows.

Style: replies are read on a phone in Telegram. Be concise and practical. Always include direct links to \
job postings or application pages when you mention them. Use simple formatting only (short paragraphs, \
"-" bullet lists); no tables or headings. Tailor everything to the user's profile below."""

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "target_roles": {"type": "array", "items": {"type": "string"}},
        "job_types": {"type": "array", "items": {"type": "string"}},
        "locations": {"type": "array", "items": {"type": "string"}},
        "skills": {"type": "array", "items": {"type": "string"}},
        "grad_year": {"type": "string"},
        "search_keywords": {"type": "array", "items": {"type": "string"}},
        "exclude": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "target_roles", "job_types", "locations", "skills", "grad_year", "search_keywords", "exclude"],
    "additionalProperties": False,
}
LIST_FIELDS = ["target_roles", "job_types", "locations", "skills", "search_keywords", "exclude"]

CUSTOM_TOOLS = [
    {
        "type": "function",
        "name": "search_jobs",
        "description": "Search live job listings in Singapore (MyCareersFuture, JSearch, Adzuna). Returns only jobs "
                       "posted within max_age_hours, newest first, with links. Keywords work best as short job titles "
                       "or fields, e.g. 'data scientist', 'machine learning intern'.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "1-4 short search terms"},
                "max_age_hours": {"type": "integer", "description": "Only jobs posted within this many hours (24 = last day, 168 = last week)"},
            },
            "required": ["keywords", "max_age_hours"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "update_profile",
        "description": "Change one field of the user's saved job-search profile, which drives the daily job digest. "
                       "Use 'add'/'remove' for list fields and 'replace' to overwrite a field.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "enum": ["summary", "grad_year"] + LIST_FIELDS},
                "action": {"type": "string", "enum": ["add", "remove", "replace"]},
                "values": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["field", "action", "values"],
            "additionalProperties": False,
        },
    },
]
WEB_SEARCH = {
    "type": "web_search",
    "user_location": {"type": "approximate", "city": "Singapore", "country": "SG", "timezone": "Asia/Singapore"},
}


class RefusalError(Exception):
    pass


def _run(input_items, instructions, tools=(), effort="medium", output_format=None, tool_handler=None, max_steps=10):
    """Call the model in a loop until it stops calling our function tools (web search runs server-side)."""
    text = {"format": {"type": "json_schema", "name": "output", "schema": output_format, "strict": True}} if output_format else openai.omit
    for _ in range(max_steps):
        resp = client().responses.create(
            model=MODEL, instructions=instructions, input=input_items, tools=list(tools) or openai.omit,
            reasoning={"effort": effort}, text=text, max_output_tokens=16000,
        )
        for item in resp.output:
            if item.type == "message" and any(c.type == "refusal" for c in item.content):
                raise RefusalError()
        input_items += resp.output
        calls = [item for item in resp.output if item.type == "function_call"]
        if not calls or not tool_handler:
            return resp
        for call in calls:
            try:
                output = tool_handler(call.name, json.loads(call.arguments))
            except Exception as e:
                log.exception("tool %s failed", call.name)
                output = f"Error: {e}"
            input_items.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
    return resp


def hours_ago(dt):
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 3600))


def _jobs_for_model(jobs, desc_limit=400):
    return json.dumps([
        {"title": j.title, "company": j.company, "location": j.location, "salary": j.salary,
         "posted_hours_ago": hours_ago(j.posted_at), "source": j.source, "url": j.url,
         "description": j.description[:desc_limit]}
        for j in jobs
    ], ensure_ascii=False)


def apply_profile_update(profile, field, action, values):
    profile = dict(profile or {})
    if field in LIST_FIELDS:
        current = list(profile.get(field) or [])
        if action == "replace":
            current = list(values)
        elif action == "add":
            current += [v for v in values if v.lower() not in {c.lower() for c in current}]
        else:
            drop = {v.lower() for v in values}
            current = [c for c in current if c.lower() not in drop]
        profile[field] = current
    else:
        profile[field] = " ".join(values)
    return profile


def chat(chat_id, text):
    """Normal conversation with tools. History is stored as plain text so it can be trimmed safely."""
    user = db.get_user(chat_id) or {}
    profile = user.get("profile") or {}

    def handle_tool(name, args):
        if name == "search_jobs":
            jobs = search_jobs(args["keywords"][:4], max(1, args["max_age_hours"]))[:25]
            return _jobs_for_model(jobs) if jobs else "No jobs found in that time window. Try broader keywords or a longer window."
        if name == "update_profile":
            current = (db.get_user(chat_id) or {}).get("profile") or {}
            db.update_user(chat_id, profile=apply_profile_update(current, args["field"], args["action"], args["values"]))
            return "Profile updated."
        return f"Unknown tool {name}"

    history = db.get_history(chat_id)
    while history and history[0]["role"] != "user":
        history.pop(0)
    now = datetime.now(SGT).strftime("%A %d %B %Y, %H:%M")
    input_items = history + [{"role": "user", "content": f"[Current time in Singapore: {now}]\n{text}"}]
    # Fixed prompt first so OpenAI's automatic prompt caching can reuse it.
    instructions = SYSTEM_PROMPT + "\n\nUser profile:\n" + json.dumps(profile, ensure_ascii=False, indent=1)
    try:
        resp = _run(input_items, instructions, CUSTOM_TOOLS + [WEB_SEARCH], effort="medium", tool_handler=handle_tool)
        reply = resp.output_text.strip() or "Sorry, I couldn't come up with a reply — try rephrasing?"
    except RefusalError:
        reply = "Sorry, I can't help with that request."
    db.add_history(chat_id, "user", text)
    db.add_history(chat_id, "assistant", reply)
    return reply


def build_profile(answers, resume_pdf=None):
    """Turn onboarding answers (+ optional resume PDF) into a structured profile dict."""
    content = []
    if resume_pdf:
        content.append({"type": "input_file", "filename": "resume.pdf",
                        "file_data": "data:application/pdf;base64," + base64.b64encode(resume_pdf).decode()})
    qa = "\n".join(f"Q: {q}\nA: {a}" for q, a in answers.items())
    content.append({"type": "input_text", "text": (
        "Build a job-search profile for this NTU DSAI undergraduate from their onboarding answers"
        + (" and the attached resume" if resume_pdf else "") + ".\n\n" + qa + "\n\n"
        "- summary: 3-5 sentences covering background, strongest skills/projects and what they're looking for.\n"
        "- search_keywords: 4-6 SHORT job-board search terms (1-3 words each, e.g. 'data scientist', "
        "'machine learning', 'data analyst intern') that together cover their targets. These are sent "
        "literally to job-board search boxes.\n"
        "- exclude: role types, industries or companies they don't want.\n"
        "- grad_year: expected graduation year, or '' if unknown.")})
    resp = _run([{"role": "user", "content": content}], "You extract structured job-search profiles.",
                effort="medium", output_format=PROFILE_SCHEMA)
    return json.loads(resp.output_text)


RANK_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "relevant": {"type": "boolean"},
            "score": {"type": "integer"},
            "summary": {"type": "string"},
        },
        "required": ["id", "relevant", "score", "summary"],
        "additionalProperties": False,
    }}},
    "required": ["results"],
    "additionalProperties": False,
}


def rank_and_summarize(profile, jobs, limit=10):
    """Return [(job, score, summary)] for the jobs that fit the profile, best first."""
    if not jobs:
        return []
    jobs = jobs[:60]
    listing = "\n\n".join(
        f"[{i}] {j.title} — {j.company} ({j.location}; {j.salary or 'salary n/a'})\n{j.description[:1200]}"
        for i, j in enumerate(jobs)
    )
    prompt = (
        "Candidate profile:\n" + json.dumps(profile, ensure_ascii=False) + "\n\nJobs:\n" + listing + "\n\n"
        "For EVERY job id: relevant = true only if it realistically fits the candidate (role, seniority for a "
        "student/fresh grad, job type, and nothing on their exclude list). score 1-10 for fit. summary = 2-3 "
        "sentences: what the role does, key requirements, and why it fits (or not). Be specific."
    )
    resp = _run([{"role": "user", "content": prompt}], "You screen job postings for a candidate.",
                effort="low", output_format=RANK_SCHEMA)
    picked = []
    for r in json.loads(resp.output_text)["results"]:
        if r["relevant"] and 0 <= r["id"] < len(jobs):
            picked.append((jobs[r["id"]], r["score"], r["summary"]))
    picked.sort(key=lambda x: x[1], reverse=True)
    return picked[:limit]


WEB_JOBS_SCHEMA = {
    "type": "object",
    "properties": {"jobs": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "company": {"type": "string"},
            "location": {"type": "string"},
            "url": {"type": "string"},
            "posted_at_utc": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["title", "company", "location", "url", "posted_at_utc", "description"],
        "additionalProperties": False,
    }}},
    "required": ["jobs"],
    "additionalProperties": False,
}


def web_job_search(profile):
    """Best-effort extra source: the model searches the web for postings from the last 24h.

    Only jobs whose posting time the model could actually see are kept; the caller still
    runs them through filter_recent, so undated or older postings are dropped.
    """
    now = datetime.now(timezone.utc)
    prompt = (
        f"Current time (UTC): {now.isoformat(timespec='minutes')}\n"
        "Candidate profile:\n" + json.dumps(profile, ensure_ascii=False) + "\n\n"
        "Search the web for job postings in Singapore (or remote roles open to Singapore) that match this "
        "candidate and were posted in the LAST 24 HOURS. Look at company career pages, LinkedIn, Indeed, "
        "Glassdoor, NodeFlair, Tech in Asia jobs and similar.\n"
        "For each job give its direct posting URL and posted_at_utc as an ISO 8601 UTC datetime computed from "
        "what the page shows (e.g. '5 hours ago' -> now minus 5h). If you cannot see when it was posted, set "
        "posted_at_utc to an empty string. Do not guess. Return at most 10 jobs; an empty list is fine."
    )
    try:
        resp = _run([{"role": "user", "content": prompt}], "You find fresh job postings on the web.",
                    tools=[WEB_SEARCH], effort="low", output_format=WEB_JOBS_SCHEMA)
        found = json.loads(resp.output_text)["jobs"]
    except Exception as e:
        log.warning("web job search failed: %s", e)
        return []
    return [Job("Web", j["title"], j["company"], j["location"], j["url"], parse_iso(j["posted_at_utc"]),
                j["description"]) for j in found]

"""Tiny SQLite storage: user profiles, onboarding state, chat history, sent jobs."""
import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "data/bot.db")

USER_FIELDS = ("profile", "onboarding_step", "onboarding_answers", "digest_hour", "last_digest_date")
JSON_FIELDS = ("profile", "onboarding_answers")


@contextmanager
def _conn():
    # A fresh connection per call keeps this safe across the bot and scheduler threads.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                profile TEXT,
                onboarding_step INTEGER,
                onboarding_answers TEXT,
                digest_hour INTEGER DEFAULT 9,
                last_digest_date TEXT
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                role TEXT,
                text TEXT
            );
            CREATE TABLE IF NOT EXISTS sent_jobs (
                chat_id INTEGER,
                job_key TEXT,
                PRIMARY KEY (chat_id, job_key)
            );
        """)


def get_user(chat_id):
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    if row is None:
        return None
    user = dict(row)
    for f in JSON_FIELDS:
        user[f] = json.loads(user[f]) if user[f] else None
    return user


def update_user(chat_id, **fields):
    """Create the user if needed, then set the given fields."""
    for f in fields:
        if f not in USER_FIELDS:
            raise ValueError(f"unknown user field: {f}")
    values = {f: json.dumps(v) if f in JSON_FIELDS and v is not None else v for f, v in fields.items()}
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        for f, v in values.items():
            c.execute(f"UPDATE users SET {f} = ? WHERE chat_id = ?", (v, chat_id))


def all_users():
    with _conn() as c:
        rows = c.execute("SELECT chat_id FROM users").fetchall()
    return [get_user(r["chat_id"]) for r in rows]


def add_history(chat_id, role, text):
    with _conn() as c:
        c.execute("INSERT INTO history (chat_id, role, text) VALUES (?, ?, ?)", (chat_id, role, text))


def get_history(chat_id, limit=20):
    with _conn() as c:
        rows = c.execute(
            "SELECT role, text FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        ).fetchall()
    return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]


def clear_history(chat_id):
    with _conn() as c:
        c.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))


def sent_job_keys(chat_id):
    with _conn() as c:
        rows = c.execute("SELECT job_key FROM sent_jobs WHERE chat_id = ?", (chat_id,)).fetchall()
    return {r["job_key"] for r in rows}


def mark_jobs_sent(chat_id, keys):
    with _conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO sent_jobs (chat_id, job_key) VALUES (?, ?)", [(chat_id, k) for k in keys]
        )

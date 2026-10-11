from dotenv import load_dotenv

load_dotenv()  # must run before importing modules that read env vars

import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime

import telebot
from apscheduler.schedulers.background import BackgroundScheduler
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

import assistant
import db
import digest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get('BOT_TOKEN')
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "").strip()

bot = telebot.TeleBot(BOT_TOKEN)

QUESTIONS = [
    "What roles or fields are you targeting? (e.g. data scientist, ML engineer, data analyst, quant)",
    "What are you looking for — internship, full-time, part-time? And from when / for how long?",
    "Preferred work locations? (e.g. anywhere in Singapore, CBD, one-north, remote OK)",
    "What are your key skills, projects and experience so far?",
    "When do you graduate, and is there anything you want to avoid (industries, companies, role types)?",
]
RESUME_STEP = len(QUESTIONS)

HELP = (
    "Just chat with me about anything job-search related — finding roles, resume feedback, interview prep, "
    "company research.\n\n"
    "/profile — view your saved profile\n"
    "/editprofile — redo the setup questions\n"
    "/digest — get today's new jobs (last 24h) now\n"
    "/settime <hour> — daily digest hour, 0-23 Singapore time\n"
    "/reset — clear our chat memory\n"
    "/help — show this message"
)


def allowed(message):
    return not ALLOWED_USER_ID or str(message.from_user.id) == ALLOWED_USER_ID


@contextmanager
def typing(chat_id):
    """Keep the 'typing…' indicator alive while a slow AI call runs."""
    done = threading.Event()

    def loop():
        while not done.is_set():
            try:
                bot.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            done.wait(4)

    threading.Thread(target=loop, daemon=True).start()
    try:
        yield
    finally:
        done.set()


def send_long(chat_id, text):
    for i in range(0, len(text), 4000):
        bot.send_message(chat_id, text[i:i + 4000], disable_web_page_preview=True)


def format_profile(p):
    lines = [p.get("summary", "")]
    for field in ["target_roles", "job_types", "locations", "skills", "search_keywords", "exclude"]:
        if p.get(field):
            lines.append(f"{field.replace('_', ' ').title()}: {', '.join(p[field])}")
    if p.get("grad_year"):
        lines.append(f"Graduating: {p['grad_year']}")
    return "\n".join(lines)


# ---------- onboarding ----------

def start_onboarding(chat_id):
    db.update_user(chat_id, onboarding_step=0, onboarding_answers={})
    bot.send_message(chat_id, "Let's set up your job-search profile — 5 quick questions, then an optional resume.\n\n"
                              "You can /skip any question.")
    bot.send_message(chat_id, QUESTIONS[0])


def record_answer(chat_id, user, answer):
    step = user["onboarding_step"]
    answers = user["onboarding_answers"] or {}
    answers[QUESTIONS[step]] = answer
    db.update_user(chat_id, onboarding_step=step + 1, onboarding_answers=answers)
    if step + 1 < RESUME_STEP:
        bot.send_message(chat_id, QUESTIONS[step + 1])
    else:
        bot.send_message(chat_id, "Last step: send your resume as a PDF so I can tailor searches to it, or /skip.")


def finish_onboarding(chat_id, resume_pdf=None):
    user = db.get_user(chat_id)
    bot.send_message(chat_id, "Building your profile…")
    with typing(chat_id):
        try:
            profile = assistant.build_profile(user["onboarding_answers"] or {}, resume_pdf)
        except Exception:
            log.exception("build_profile failed")
            bot.send_message(chat_id, "Something went wrong building your profile. Send /skip to try again.")
            return
    db.update_user(chat_id, profile=profile, onboarding_step=None, onboarding_answers=None)
    hour = user["digest_hour"] if user["digest_hour"] is not None else 9
    send_long(chat_id, "Here's your profile:\n\n" + format_profile(profile))
    bot.send_message(chat_id, f"I'll send new matching jobs (posted in the last 24h) every day at {hour}:00 "
                              f"Singapore time. Send /digest to get them now, or /editprofile anytime to redo this.\n\n"
                              + HELP)


# ---------- digest ----------

def send_digest(chat_id):
    with typing(chat_id):
        picked = digest.build_digest(chat_id)
    if not picked:
        bot.send_message(chat_id, "🗓 No new matching jobs were posted in the last 24 hours. I'll check again tomorrow.")
        return
    bot.send_message(chat_id, f"🗓 {len(picked)} new matching job{'s' if len(picked) > 1 else ''} posted in the last 24 hours:")
    for job, _score, summary in picked:
        markup = None
        if job.url.startswith("http"):
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("Apply / View", url=job.url))
        bot.send_message(chat_id, digest.format_job(job, summary), parse_mode="HTML",
                         reply_markup=markup, disable_web_page_preview=True)


def scheduled_digests():
    now = datetime.now(assistant.SGT)
    today = now.date().isoformat()
    for user in db.all_users():
        # ">=" so a digest missed during a restart/redeploy still goes out later that day.
        if (user["profile"] and user["onboarding_step"] is None and user["last_digest_date"] != today
                and now.hour >= (user["digest_hour"] if user["digest_hour"] is not None else 9)):
            db.update_user(user["chat_id"], last_digest_date=today)
            try:
                send_digest(user["chat_id"])
            except Exception:
                log.exception("digest failed for %s", user["chat_id"])


# ---------- handlers ----------

@bot.message_handler(func=lambda m: not allowed(m), content_types=["text", "document", "photo"])
def reject(message):
    bot.reply_to(message, "Sorry, this is a private assistant.")


@bot.message_handler(commands=["start"])
def cmd_start(message):
    user = db.get_user(message.chat.id)
    if user and user["profile"]:
        bot.send_message(message.chat.id, "Welcome back! 👋\n\n" + HELP)
    else:
        bot.send_message(message.chat.id, "Hi! I'm your personal job-search assistant. 🎯")
        start_onboarding(message.chat.id)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id, HELP)


@bot.message_handler(commands=["editprofile"])
def cmd_editprofile(message):
    start_onboarding(message.chat.id)


@bot.message_handler(commands=["profile"])
def cmd_profile(message):
    user = db.get_user(message.chat.id)
    if not user or not user["profile"]:
        bot.send_message(message.chat.id, "No profile yet — send /start to set one up.")
        return
    send_long(message.chat.id, format_profile(user["profile"]))


@bot.message_handler(commands=["skip"])
def cmd_skip(message):
    user = db.get_user(message.chat.id)
    if not user or user["onboarding_step"] is None:
        bot.send_message(message.chat.id, "Nothing to skip right now.")
    elif user["onboarding_step"] >= RESUME_STEP:
        finish_onboarding(message.chat.id)
    else:
        record_answer(message.chat.id, user, "(skipped)")


@bot.message_handler(commands=["digest"])
def cmd_digest(message):
    user = db.get_user(message.chat.id)
    if not user or not user["profile"]:
        bot.send_message(message.chat.id, "Set up your profile first with /start.")
        return
    bot.send_message(message.chat.id, "Searching for jobs posted in the last 24 hours… this can take a minute.")
    try:
        send_digest(message.chat.id)
    except Exception:
        log.exception("manual digest failed")
        bot.send_message(message.chat.id, "Sorry, the job search failed. Please try again in a bit.")


@bot.message_handler(commands=["settime"])
def cmd_settime(message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit() or not 0 <= int(parts[1]) <= 23:
        bot.send_message(message.chat.id, "Usage: /settime <hour 0-23>, e.g. /settime 8 for 8am Singapore time.")
        return
    db.update_user(message.chat.id, digest_hour=int(parts[1]))
    bot.send_message(message.chat.id, f"Done — daily digest at {int(parts[1])}:00 Singapore time.")


@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    db.clear_history(message.chat.id)
    bot.send_message(message.chat.id, "Chat memory cleared. Your profile is unchanged.")


@bot.message_handler(content_types=["document"])
def on_document(message):
    user = db.get_user(message.chat.id)
    if not user or user["onboarding_step"] != RESUME_STEP:
        bot.send_message(message.chat.id, "To update your profile with a new resume, send /editprofile.")
        return
    if message.document.mime_type != "application/pdf":
        bot.send_message(message.chat.id, "Please send your resume as a PDF, or /skip.")
        return
    pdf = bot.download_file(bot.get_file(message.document.file_id).file_path)
    finish_onboarding(message.chat.id, pdf)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(message):
    user = db.get_user(message.chat.id)
    if user is None:
        cmd_start(message)
        return
    step = user["onboarding_step"]
    if step is not None and step < RESUME_STEP:
        record_answer(message.chat.id, user, message.text)
        return
    if step == RESUME_STEP:
        bot.send_message(message.chat.id, "Please send your resume as a PDF, or /skip.")
        return
    try:
        with typing(message.chat.id):
            reply = assistant.chat(message.chat.id, message.text)
    except Exception:
        log.exception("chat failed")
        reply = "Sorry, something went wrong on my side. Please try again."
    send_long(message.chat.id, reply)


if __name__ == "__main__":
    db.init_db()
    if not ALLOWED_USER_ID:
        log.warning("ALLOWED_USER_ID is not set — anyone who finds this bot can use it (and your API credits).")
    scheduler = BackgroundScheduler(timezone="Asia/Singapore")
    scheduler.add_job(scheduled_digests, "cron", minute=0)
    scheduler.start()
    bot.set_my_commands([
        telebot.types.BotCommand("digest", "New jobs from the last 24h"),
        telebot.types.BotCommand("profile", "View your profile"),
        telebot.types.BotCommand("editprofile", "Redo profile setup"),
        telebot.types.BotCommand("settime", "Set daily digest hour"),
        telebot.types.BotCommand("reset", "Clear chat memory"),
        telebot.types.BotCommand("help", "How to use this bot"),
    ])
    log.info("Bot running")
    bot.infinity_polling()

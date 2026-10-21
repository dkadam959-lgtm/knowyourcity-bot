"""
Know Your City - Telegram quiz bot
----------------------------------
Daily 5 questions sent as Telegram quiz polls in private chat.
Scores, daily/weekly/monthly leaderboards, channel posts, reminders,
referral bonus, admin CSV upload.

Settings come from environment variables (see .env.example).
"""

import asyncio
import csv
import io
import logging
import os
import sqlite3
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    PollAnswerHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")            # e.g. @KnowYourCity
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Kolkata"))
DATA_DIR = os.environ.get("DATA_DIR", "./data")
SPONSOR_TEXT = os.environ.get("SPONSOR_TEXT", "")       # e.g. "सौजन्य अमुक क्लासेस"
START_DATE = date.fromisoformat(os.environ.get("START_DATE", "2026-10-27"))

QUIZ_HOUR = int(os.environ.get("QUIZ_HOUR", "8"))        # day starts 08:00
REMIND_HOUR = int(os.environ.get("REMIND_HOUR", "15"))   # reminder 15:00
RESULT_HOUR = int(os.environ.get("RESULT_HOUR", "21"))   # answers 21:00

QUESTIONS_PER_DAY = 5
POINTS_CORRECT = 10
POLL_OPEN_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))   # question closes after this
SPEED_BONUS = [(10, 5), (20, 3), (30, 1)]   # (within seconds, bonus points)
REFERRAL_BONUS = 5
STREAK_BONUS_DAYS = 7      # every 7 consecutive days
STREAK_BONUS = 10

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "kyc.db")
QUESTIONS_PATH = os.path.join(DATA_DIR, "questions.csv")
if not os.path.exists(QUESTIONS_PATH) and os.path.exists("questions.csv"):
    import shutil
    shutil.copy("questions.csv", QUESTIONS_PATH)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("kyc")

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT, username TEXT,
                joined TEXT, source TEXT,
                remind INTEGER DEFAULT 1,
                referred_by INTEGER,
                referral_paid INTEGER DEFAULT 0,
                bonus INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS polls (
                poll_id TEXT PRIMARY KEY,
                user_id INTEGER, qday TEXT, qid INTEGER, qindex INTEGER,
                sent_ts REAL, answered INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS answers (
                user_id INTEGER, qday TEXT, qid INTEGER,
                correct INTEGER, seconds REAL, points INTEGER,
                ts TEXT,
                PRIMARY KEY (user_id, qday, qid)
            );
            CREATE INDEX IF NOT EXISTS idx_answers_day ON answers(qday);
            """
        )


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------
QUESTIONS = []   # list of dicts


def load_questions():
    global QUESTIONS
    rows = []
    with open(QUESTIONS_PATH, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                q = {
                    "id": int(r["id"]),
                    "category": r.get("category", "").strip(),
                    "question": r["question"].strip(),
                    "options": [r["option_a"].strip(), r["option_b"].strip(),
                                r["option_c"].strip(), r["option_d"].strip()],
                    "correct": "ABCD".index(r["correct"].strip().upper()),
                    "explanation": r.get("explanation", "").strip(),
                    "source": (r.get("source") or "").strip(),
                }
                if q["question"] and all(q["options"]):
                    rows.append(q)
            except Exception as e:
                log.warning("bad row %s: %s", r, e)
    QUESTIONS = rows
    log.info("loaded %d questions", len(QUESTIONS))


def now():
    return datetime.now(TZ)


def quiz_day(dt=None) -> str:
    """Day key. The quiz day runs QUIZ_HOUR to QUIZ_HOUR next day."""
    dt = dt or now()
    return (dt - timedelta(hours=QUIZ_HOUR)).date().isoformat()


def questions_for_day(qday: str):
    """Deterministic rotation: 5 questions per day in CSV order, wraps around."""
    if not QUESTIONS:
        return []
    d = date.fromisoformat(qday)
    idx = max(0, (d - START_DATE).days)
    n = len(QUESTIONS)
    start = (idx * QUESTIONS_PER_DAY) % n
    out = []
    for i in range(QUESTIONS_PER_DAY):
        out.append(QUESTIONS[(start + i) % n])
    return out


def qbyid(qid):
    for q in QUESTIONS:
        if q["id"] == qid:
            return q
    return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def day_label(qday: str):
    d = date.fromisoformat(qday)
    return f"{d.day:02d}-{d.month:02d}-{d.year}"


def day_number(qday: str):
    return (date.fromisoformat(qday) - START_DATE).days + 1


def mention(user_id, name):
    safe = (name or "सभासद").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def leaderboard(period: str, limit=10):
    """period: day | week | month. Returns list of (user_id, name, points, seconds)."""
    today = date.fromisoformat(quiz_day())
    if period == "day":
        lo = hi = today
    elif period == "week":
        lo = today - timedelta(days=today.weekday())
        hi = today
    else:
        lo = today.replace(day=1)
        hi = today
    with db() as con:
        rows = con.execute(
            """
            SELECT a.user_id, u.name,
                   SUM(a.points) + CASE WHEN ?='day' THEN 0 ELSE COALESCE(u.bonus,0) END AS pts,
                   SUM(a.seconds) AS secs
            FROM answers a JOIN users u ON u.user_id=a.user_id
            WHERE a.qday BETWEEN ? AND ?
            GROUP BY a.user_id
            ORDER BY pts DESC, secs ASC
            LIMIT ?
            """,
            (period, lo.isoformat(), hi.isoformat(), limit),
        ).fetchall()
    return rows


def user_rank_today(user_id):
    with db() as con:
        rows = con.execute(
            """
            SELECT user_id, SUM(points) pts, SUM(seconds) secs
            FROM answers WHERE qday=?
            GROUP BY user_id ORDER BY pts DESC, secs ASC
            """,
            (quiz_day(),),
        ).fetchall()
    for i, r in enumerate(rows, 1):
        if r["user_id"] == user_id:
            return i, r["pts"], r["secs"], len(rows)
    return None, 0, 0, len(rows)


def answered_count(user_id, qday):
    with db() as con:
        return con.execute(
            "SELECT COUNT(*) FROM answers WHERE user_id=? AND qday=?", (user_id, qday)
        ).fetchone()[0]


def sent_count(user_id, qday):
    with db() as con:
        return con.execute(
            "SELECT COUNT(*) FROM polls WHERE user_id=? AND qday=?", (user_id, qday)
        ).fetchone()[0]


async def safe_send(bot, chat_id, text, **kw):
    """Send with rate-limit handling. Returns True if delivered."""
    for _ in range(3):
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True, **kw)
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Forbidden:
            with db() as con:
                con.execute("UPDATE users SET remind=0 WHERE user_id=?", (chat_id,))
            return False
        except BadRequest as e:
            log.warning("send failed %s: %s", chat_id, e)
            return False
    return False


# --------------------------------------------------------------------------
# Quiz flow
# --------------------------------------------------------------------------
async def send_next_question(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    qday = quiz_day()
    qs = questions_for_day(qday)
    idx = sent_count(user_id, qday)
    if idx >= len(qs):
        await send_result(context, user_id)
        return
    q = qs[idx]
    expl = q["explanation"]
    if q["source"]:
        expl = (expl + "\nस्रोत " + q["source"]).strip()
    try:
        msg = await context.bot.send_poll(
            chat_id=user_id,
            question=f"प्रश्न {idx + 1}/{len(qs)}  ({POLL_OPEN_SECONDS} सेकंद)\n{q['question']}",
            options=q["options"],
            type="quiz",
            correct_option_id=q["correct"],
            is_anonymous=False,
            explanation=expl[:200] if expl else None,
            open_period=POLL_OPEN_SECONDS,
        )
    except Forbidden:
        return
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO polls(poll_id,user_id,qday,qid,qindex,sent_ts) VALUES(?,?,?,?,?,?)",
            (msg.poll.id, user_id, qday, q["id"], idx, now().timestamp()),
        )
    # if no answer by the deadline, mark 0 and move on
    context.job_queue.run_once(
        job_poll_timeout, POLL_OPEN_SECONDS + 2,
        data={"poll_id": msg.poll.id, "user_id": user_id, "qday": qday, "qid": q["id"]},
        name=f"timeout_{msg.poll.id}",
    )


async def job_poll_timeout(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    with db() as con:
        p = con.execute("SELECT answered FROM polls WHERE poll_id=?", (d["poll_id"],)).fetchone()
        if not p or p["answered"]:
            return
        con.execute("UPDATE polls SET answered=1 WHERE poll_id=?", (d["poll_id"],))
        con.execute(
            "INSERT OR REPLACE INTO answers(user_id,qday,qid,correct,seconds,points,ts) VALUES(?,?,?,?,?,?,?)",
            (d["user_id"], d["qday"], d["qid"], 0, POLL_OPEN_SECONDS, 0, now().isoformat()),
        )
    await safe_send(context.bot, d["user_id"], "वेळ संपली. हा प्रश्न ० गुण. पुढचा प्रश्न येतोय.")
    await asyncio.sleep(0.8)
    await send_next_question(context, d["user_id"])


def streak_days(user_id) -> int:
    """Consecutive quiz days (ending today or yesterday) with all questions answered."""
    with db() as con:
        rows = con.execute(
            "SELECT qday FROM answers WHERE user_id=? GROUP BY qday HAVING COUNT(*)>=? ORDER BY qday DESC",
            (user_id, QUESTIONS_PER_DAY)).fetchall()
    days = {r["qday"] for r in rows}
    d = date.fromisoformat(quiz_day())
    if d.isoformat() not in days:
        d -= timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    user = pa.user
    with db() as con:
        p = con.execute("SELECT * FROM polls WHERE poll_id=?", (pa.poll_id,)).fetchone()
    if not p or p["answered"]:
        return
    q = qbyid(p["qid"])
    if not q or not pa.option_ids:
        return
    seconds = max(0.0, now().timestamp() - p["sent_ts"])
    correct = 1 if pa.option_ids[0] == q["correct"] else 0
    points = 0
    if correct:
        points = POINTS_CORRECT
        for within, bonus in SPEED_BONUS:
            if seconds <= within:
                points += bonus
                break
    with db() as con:
        con.execute("UPDATE polls SET answered=1 WHERE poll_id=?", (pa.poll_id,))
        con.execute(
            "INSERT OR REPLACE INTO answers(user_id,qday,qid,correct,seconds,points,ts) VALUES(?,?,?,?,?,?,?)",
            (user.id, p["qday"], p["qid"], correct, round(seconds, 1), points, now().isoformat()),
        )
    for j in context.job_queue.get_jobs_by_name(f"timeout_{pa.poll_id}"):
        j.schedule_removal()
    # referral bonus when a referred user finishes their first full day
    await maybe_pay_referral(context, user.id)
    await asyncio.sleep(0.8)
    await send_next_question(context, user.id)


async def maybe_pay_referral(context, user_id):
    with db() as con:
        u = con.execute("SELECT referred_by, referral_paid FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not u or not u["referred_by"] or u["referral_paid"]:
            return
        if answered_count(user_id, quiz_day()) < QUESTIONS_PER_DAY:
            return
        con.execute("UPDATE users SET referral_paid=1 WHERE user_id=?", (user_id,))
        con.execute("UPDATE users SET bonus=COALESCE(bonus,0)+? WHERE user_id=?", (REFERRAL_BONUS, u["referred_by"]))
    await safe_send(context.bot, u["referred_by"],
                    f"तुमच्या आमंत्रणाने आलेल्या मित्राने आजची स्पर्धा पूर्ण केली. तुम्हाला {REFERRAL_BONUS} बोनस गुण मिळाले.")


async def send_result(context, user_id):
    rank, pts, secs, total = user_rank_today(user_id)
    top = leaderboard("day", 3)
    streak = streak_days(user_id)
    streak_line = f"सलग दिवस <b>{streak}</b>"
    if streak and streak % STREAK_BONUS_DAYS == 0:
        with db() as con:
            con.execute("UPDATE users SET bonus=COALESCE(bonus,0)+? WHERE user_id=?", (STREAK_BONUS, user_id))
        streak_line += f"  सलग {streak} दिवसांचा बोनस {STREAK_BONUS} गुण"
    lines = [f"<b>आजचा निकाल</b>  दिवस {day_number(quiz_day())}",
             f"गुण <b>{pts}</b> / {QUESTIONS_PER_DAY * (POINTS_CORRECT + SPEED_BONUS[0][1])}",
             f"वेळ {secs:.0f} सेकंद",
             f"आजचा नंबर <b>{rank}</b> ({total} खेळाडूंमध्ये)",
             streak_line, ""]
    if top:
        lines.append("आजचे आघाडीवर")
        for i, r in enumerate(top, 1):
            lines.append(f"{i}. {mention(r['user_id'], r['name'])}  {r['pts']}")
    lines += ["", f"उत्तरं आणि विजेते रात्री {RESULT_HOUR} वाजता चॅनलवर.",
              f"मित्राला आमंत्रण द्या आणि {REFERRAL_BONUS} बोनस गुण मिळवा."]
    me = await context.bot.get_me()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("मित्राला पाठवा",
                              url=f"https://t.me/share/url?url=https://t.me/{me.username}?start=ref_{user_id}&text=रोज%205%20प्रश्न.%20तुमचं%20शहर%20किती%20ओळखता%3F")],
        [InlineKeyboardButton("माझा महिन्याचा हिशेब", callback_data="top_month")],
    ])
    await safe_send(context.bot, user_id, "\n".join(lines), reply_markup=kb)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    arg = context.args[0] if context.args else ""
    source = arg
    referred_by = None
    if arg.startswith("ref_"):
        try:
            referred_by = int(arg[4:])
            if referred_by == user.id:
                referred_by = None
        except ValueError:
            pass
    with db() as con:
        exists = con.execute("SELECT 1 FROM users WHERE user_id=?", (user.id,)).fetchone()
        if not exists:
            con.execute(
                "INSERT INTO users(user_id,name,username,joined,source,referred_by) VALUES(?,?,?,?,?,?)",
                (user.id, user.full_name, user.username, now().isoformat(), source, referred_by),
            )
        else:
            con.execute("UPDATE users SET name=?, username=? WHERE user_id=?",
                        (user.full_name, user.username, user.id))
    done = answered_count(user.id, quiz_day())
    if done >= QUESTIONS_PER_DAY:
        await update.message.reply_text(
            f"नमस्कार {user.first_name}. आजची स्पर्धा तुम्ही पूर्ण केली आहे. उद्या सकाळी {QUIZ_HOUR} वाजता पुढचे प्रश्न येतील.\n/score माझे गुण  /top आजची यादी")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("आजची स्पर्धा खेळा", callback_data="play")]])
    text = (f"नमस्कार {user.first_name}. <b>Know Your City</b> मध्ये स्वागत.\n\n"
            f"रोज सकाळी {QUIZ_HOUR} वाजता तुमच्या शहराबद्दल {QUESTIONS_PER_DAY} प्रश्न. "
            f"बरोबर उत्तराला {POINTS_CORRECT} गुण आणि वेगाचा बोनस {SPEED_BONUS[0][1]} पर्यंत. "
            f"रोज आठवड्याला आणि महिन्याला विजेते जाहीर होतात.\n\n"
            + (f"{SPONSOR_TEXT}\n\n" if SPONSOR_TEXT else "")
            + "तयार असाल तर खालचं बटण दाबा.")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    if answered_count(user_id, quiz_day()) >= QUESTIONS_PER_DAY:
        await q.edit_message_text("आजची स्पर्धा तुम्ही पूर्ण केली आहे. उद्या भेटू.")
        return
    try:
        await q.edit_message_reply_markup(None)
    except BadRequest:
        pass
    await send_next_question(context, user_id)


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db() as con:
        if not con.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
            return await cmd_start(update, context)
    if answered_count(user_id, quiz_day()) >= QUESTIONS_PER_DAY:
        await update.message.reply_text("आजची स्पर्धा तुम्ही पूर्ण केली आहे. उद्या भेटू.")
        return
    await send_next_question(context, user_id)


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rank, pts, secs, total = user_rank_today(user_id)
    today = date.fromisoformat(quiz_day())
    with db() as con:
        wk = con.execute("SELECT COALESCE(SUM(points),0) FROM answers WHERE user_id=? AND qday>=?",
                         (user_id, (today - timedelta(days=today.weekday())).isoformat())).fetchone()[0]
        mo = con.execute("SELECT COALESCE(SUM(points),0) FROM answers WHERE user_id=? AND qday>=?",
                         (user_id, today.replace(day=1).isoformat())).fetchone()[0]
        bonus = con.execute("SELECT COALESCE(bonus,0) FROM users WHERE user_id=?", (user_id,)).fetchone()
        days = con.execute("SELECT COUNT(DISTINCT qday) FROM answers WHERE user_id=?", (user_id,)).fetchone()[0]
    bonus = bonus[0] if bonus else 0
    text = (f"<b>माझे गुण</b>\n"
            f"आज {pts} गुण" + (f"  नंबर {rank}" if rank else "  (अजून खेळला नाही)") + "\n"
            f"या आठवड्यात {wk}\nया महिन्यात {mo + bonus} (बोनस {bonus})\n"
            f"एकूण खेळलेले दिवस {days}\nसलग दिवस {streak_days(user_id)}")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def fmt_board(title, rows):
    if not rows:
        return f"<b>{title}</b>\nअजून कोणी खेळलं नाही."
    lines = [f"<b>{title}</b>"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {mention(r['user_id'], r['name'])}  {r['pts']} गुण  {r['secs']:.0f} से.")
    return "\n".join(lines)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    period = (context.args[0] if context.args else "day").lower()
    if period not in ("day", "week", "month"):
        period = "day"
    title = {"day": "आजचे आघाडीवर", "week": "या आठवड्याचे आघाडीवर", "month": "या महिन्याचे आघाडीवर"}[period]
    await update.message.reply_text(fmt_board(title, leaderboard(period)), parse_mode=ParseMode.HTML)


async def on_top_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_send(context.bot, q.from_user.id, fmt_board("या महिन्याचे आघाडीवर", leaderboard("month")))


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    arg = (context.args[0] if context.args else "").lower()
    if arg in ("off", "बंद"):
        val, msg = 0, "आठवण बंद केली. /remind on ने पुन्हा सुरू करता येईल."
    else:
        val, msg = 1, f"रोज दुपारी {REMIND_HOUR} वाजता न खेळल्यास आठवण येईल."
    with db() as con:
        con.execute("UPDATE users SET remind=? WHERE user_id=?", (val, user_id))
    await update.message.reply_text(msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/play आजचे प्रश्न\n/score माझे गुण\n/top आजची यादी\n/top week आठवड्याची यादी\n"
        "/top month महिन्याची यादी\n/remind off आठवण बंद\n/remind on आठवण सुरू")


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
def is_admin(update: Update):
    return update.effective_user and update.effective_user.id in ADMIN_IDS


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    with db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today = con.execute("SELECT COUNT(DISTINCT user_id) FROM answers WHERE qday=?", (quiz_day(),)).fetchone()[0]
        src = con.execute("SELECT source, COUNT(*) c FROM users GROUP BY source ORDER BY c DESC LIMIT 8").fetchall()
    lines = [f"एकूण सभासद {users}", f"आज खेळले {today}", f"प्रश्न साठा {len(QUESTIONS)}", "", "कुठून आले"]
    for r in src:
        lines.append(f"{r['source'] or 'direct'}: {r['c']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/preview  or  /preview 2026-11-03  shows that day's questions to admin."""
    if not is_admin(update):
        return
    qday = context.args[0] if context.args else quiz_day()
    qs = questions_for_day(qday)
    lines = [f"दिवस {day_number(qday)}  {day_label(qday)}"]
    for i, q in enumerate(qs, 1):
        lines.append(f"\n{i}. {q['question']}\n   उत्तर {'अबकड'[q['correct']]}. {q['options'][q['correct']]}")
    await update.message.reply_text("\n".join(lines))


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /post morning | /post evening  (manual trigger)."""
    if not is_admin(update):
        return
    what = context.args[0] if context.args else "morning"
    if what == "evening":
        await job_evening(context)
    elif what == "remind":
        await job_reminder(context)
    else:
        await job_morning(context)
    await update.message.reply_text("झालं.")


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /backup sends the database file and the questions file."""
    if not is_admin(update):
        return
    with open(DB_PATH, "rb") as f:
        await update.message.reply_document(f, filename=f"kyc-backup-{now():%Y-%m-%d}.db")
    with open(QUESTIONS_PATH, "rb") as f:
        await update.message.reply_document(f, filename="questions.csv")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /broadcast <message>  sends a message to every registered user."""
    if not is_admin(update):
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("वापर: /broadcast तुमचा मेसेज")
        return
    with db() as con:
        users = con.execute("SELECT user_id FROM users WHERE remind=1").fetchall()
    n = 0
    for u in users:
        if await safe_send(context.bot, u["user_id"], text):
            n += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"{n} जणांना पाठवलं.")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /export sends a CSV of users and total points (for prizes and sponsors)."""
    if not is_admin(update):
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "username", "joined", "source", "days_played", "total_points"])
    with db() as con:
        rows = con.execute(
            """SELECT u.user_id, u.name, u.username, u.joined, u.source,
                      COUNT(DISTINCT a.qday) d, COALESCE(SUM(a.points),0)+COALESCE(u.bonus,0) p
               FROM users u LEFT JOIN answers a ON a.user_id=u.user_id
               GROUP BY u.user_id ORDER BY p DESC"""
        ).fetchall()
    for r in rows:
        w.writerow([r["user_id"], r["name"], r["username"], r["joined"], r["source"], r["d"], r["p"]])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = f"users-{now():%Y-%m-%d}.csv"
    await update.message.reply_document(data)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends questions.csv as a file to replace the question bank."""
    if not is_admin(update):
        return
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".csv"):
        return
    f = await doc.get_file()
    data = await f.download_as_bytearray()
    text = bytes(data).decode("utf-8-sig")
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
        need = {"id", "question", "option_a", "option_b", "option_c", "option_d", "correct"}
        if not rows or not need.issubset(set(rows[0].keys())):
            await update.message.reply_text("CSV मध्ये हे कॉलम हवेत: " + ", ".join(sorted(need)))
            return
    except Exception as e:
        await update.message.reply_text(f"CSV वाचता आली नाही: {e}")
        return
    with open(QUESTIONS_PATH, "w", encoding="utf-8") as out:
        out.write(text)
    load_questions()
    await update.message.reply_text(f"प्रश्न साठा बदलला. आता {len(QUESTIONS)} प्रश्न.")


# --------------------------------------------------------------------------
# Scheduled jobs
# --------------------------------------------------------------------------
async def job_morning(context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_ID:
        return
    me = await context.bot.get_me()
    qday = quiz_day()
    cats = sorted({q["category"] for q in questions_for_day(qday) if q["category"]})
    text = (f"<b>आजची स्पर्धा सुरू</b>\n"
            f"दिवस {day_number(qday)}  {day_label(qday)}\n"
            f"{QUESTIONS_PER_DAY} प्रश्न" + (f"  विषय {', '.join(cats)}" if cats else "") + "\n"
            f"उद्या सकाळी {QUIZ_HOUR} पर्यंत खेळा. उत्तरं आणि विजेते रात्री {RESULT_HOUR} वाजता."
            + (f"\n\n{SPONSOR_TEXT}" if SPONSOR_TEXT else ""))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("आजची स्पर्धा खेळा", url=f"https://t.me/{me.username}?start=channel")]])
    try:
        await context.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.error("channel post failed: %s", e)


async def job_reminder(context: ContextTypes.DEFAULT_TYPE):
    qday = quiz_day()
    with db() as con:
        users = con.execute(
            "SELECT user_id FROM users WHERE remind=1 AND user_id NOT IN "
            "(SELECT user_id FROM answers WHERE qday=?)", (qday,)).fetchall()
    me = await context.bot.get_me()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("आता खेळा", callback_data="play")]])
    hours_left = (QUIZ_HOUR + 24 - now().hour) % 24
    text = f"आजची स्पर्धा अजून बाकी आहे. {hours_left} तास उरले. दोन मिनिटांत {QUESTIONS_PER_DAY} प्रश्न."
    n = 0
    for u in users:
        if await safe_send(context.bot, u["user_id"], text, reply_markup=kb):
            n += 1
        await asyncio.sleep(0.05)   # ~20 msg/sec, under Telegram limit
    log.info("reminders sent: %d", n)


async def job_evening(context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_ID:
        return
    qday = quiz_day()
    qs = questions_for_day(qday)
    lines = [f"<b>आजची उत्तरं</b>  दिवस {day_number(qday)}"]
    for i, q in enumerate(qs, 1):
        lines.append(f"\n{i}. {q['question']}\n✔ {q['options'][q['correct']]}"
                     + (f"\n{q['explanation']}" if q["explanation"] else ""))
    with db() as con:
        played = con.execute("SELECT COUNT(DISTINCT user_id) FROM answers WHERE qday=?", (qday,)).fetchone()[0]
    lines.append(f"\nआज {played} जणांनी खेळलं.")
    lines.append("\n" + fmt_board("आजचे विजेते", leaderboard("day", 10)))
    if date.fromisoformat(qday).weekday() == 6:
        lines.append("\n" + fmt_board("या आठवड्याचे विजेते", leaderboard("week", 5)))
    if SPONSOR_TEXT:
        lines.append(f"\n{SPONSOR_TEXT}")
    try:
        await context.bot.send_message(CHANNEL_ID, "\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error("evening post failed: %s", e)


async def job_morning_push(context: ContextTypes.DEFAULT_TYPE):
    """Send the morning kick-off to every registered user (private chat)."""
    with db() as con:
        users = con.execute("SELECT user_id FROM users WHERE remind=1").fetchall()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("आजची स्पर्धा खेळा", callback_data="play")]])
    text = f"सुप्रभात. आजचे {QUESTIONS_PER_DAY} प्रश्न तयार आहेत. दिवस {day_number(quiz_day())}."
    for u in users:
        await safe_send(context.bot, u["user_id"], text, reply_markup=kb)
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("play", "आजचे प्रश्न"),
        BotCommand("score", "माझे गुण"),
        BotCommand("top", "आजची यादी"),
        BotCommand("remind", "आठवण सुरू किंवा बंद"),
        BotCommand("help", "मदत"),
    ])
    jq = app.job_queue
    jq.run_daily(job_morning, time(QUIZ_HOUR, 0, tzinfo=TZ), name="morning")
    jq.run_daily(job_morning_push, time(QUIZ_HOUR, 1, tzinfo=TZ), name="morning_push")
    jq.run_daily(job_reminder, time(REMIND_HOUR, 0, tzinfo=TZ), name="reminder")
    jq.run_daily(job_evening, time(RESULT_HOUR, 0, tzinfo=TZ), name="evening")
    log.info("jobs scheduled; questions=%d; channel=%s; admins=%s", len(QUESTIONS), CHANNEL_ID, ADMIN_IDS)


def main():
    init_db()
    load_questions()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("play", cmd_play))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("preview", cmd_preview))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_play, pattern="^play$"))
    app.add_handler(CallbackQueryHandler(on_top_month, pattern="^top_month$"))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, on_document))
    log.info("starting polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

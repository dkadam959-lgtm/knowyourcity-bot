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

# Google Sheet as the master question list (optional). Any of these forms work:
#   full sheet URL https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0
#   or just the <ID>
SHEET_URL = os.environ.get("SHEET_URL", "").strip()
SHEET_SYNC_MINUTES = int(os.environ.get("SHEET_SYNC_MINUTES", "10"))

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
            CREATE TABLE IF NOT EXISTS extra_questions (
                id INTEGER PRIMARY KEY,
                category TEXT, question TEXT,
                option_a TEXT, option_b TEXT, option_c TEXT, option_d TEXT,
                correct INTEGER, explanation TEXT, image TEXT,
                qdate TEXT, created TEXT
            );
            """
        )


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------
QUESTIONS = []   # list of dicts


def read_rows(path):
    """Read question rows from .csv or .xlsx. Returns list of dicts with str values."""
    if path.lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(it)]
        out = []
        for row in it:
            if row is None or all(v in (None, "") for v in row):
                continue
            d = {}
            for h, v in zip(header, row):
                if h:
                    d[h] = "" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
            out.append(d)
        return out
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


import hashlib
import re
import urllib.request


def sheet_csv_url():
    """Turn a Google Sheet link or id into its CSV export link."""
    if not SHEET_URL:
        return ""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", SHEET_URL)
    sid = m.group(1) if m else SHEET_URL
    gid = "0"
    g = re.search(r"[#&?]gid=(\d+)", SHEET_URL)
    if g:
        gid = g.group(1)
    return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"


def fetch_url(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 KnowYourCityBot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sync_sheet() -> str:
    """Download the Google Sheet as CSV. Returns 'changed', 'same', or an error text."""
    url = sheet_csv_url()
    if not url:
        return "no sheet"
    try:
        data = fetch_url(url)
    except Exception as e:
        return f"error {e}"
    if data[:1] == b"<":
        return "error sheet is not shared. Share it as Anyone with the link can view."
    new_hash = hashlib.md5(data).hexdigest()
    old_hash = ""
    if os.path.exists(QUESTIONS_PATH):
        with open(QUESTIONS_PATH, "rb") as f:
            old_hash = hashlib.md5(f.read()).hexdigest()
    if new_hash == old_hash:
        return "same"
    # validate before replacing
    tmp = QUESTIONS_PATH + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        rows = read_rows(tmp)
        need = {"id", "question", "option_a", "option_b", "option_c", "option_d", "correct"}
        if not rows or not need.issubset(set(rows[0].keys())):
            return "error sheet columns missing"
    except Exception as e:
        return f"error {e}"
    os.replace(tmp, QUESTIONS_PATH)
    load_questions()
    return "changed"


def drive_direct(url: str) -> str:
    """Google Drive share link -> direct download link."""
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


def image_bytes(img: str):
    """Return (bytes, path) for an image reference: URL (incl. Drive) or a local file name. Cached."""
    os.makedirs(os.path.join(DATA_DIR, "images"), exist_ok=True)
    if img.startswith("http"):
        key = hashlib.md5(img.encode()).hexdigest()[:16] + ".img"
        path = os.path.join(DATA_DIR, "images", key)
        if not os.path.exists(path):
            data = fetch_url(drive_direct(img), timeout=60)
            if data[:1] == b"<":
                raise ValueError("image link is not public")
            with open(path, "wb") as f:
                f.write(data)
        return path
    local = os.path.join(DATA_DIR, "images", img)
    return local if os.path.exists(local) else None


def norm_date(s: str) -> str:
    """Accept 2026-11-05, 05-11-2026, 5/11/2026 -> 2026-11-05. Empty if not a date."""
    s = s.strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def extra_questions(qdate=None):
    """Questions added in Telegram. qdate=None -> undated ones (go to the rotation pool)."""
    with db() as con:
        if qdate is None:
            rows = con.execute("SELECT * FROM extra_questions WHERE qdate IS NULL OR qdate='' ORDER BY id").fetchall()
        else:
            rows = con.execute("SELECT * FROM extra_questions WHERE qdate=? ORDER BY id", (qdate,)).fetchall()
    return [{
        "id": r["id"], "category": r["category"] or "", "question": r["question"],
        "options": [r["option_a"], r["option_b"], r["option_c"], r["option_d"]],
        "correct": r["correct"], "explanation": r["explanation"] or "", "source": "",
        "image": r["image"] or "", "date": r["qdate"] or "",
    } for r in rows]


def load_questions():
    global QUESTIONS
    rows = []
    for r in read_rows(QUESTIONS_PATH):
        if True:
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
                    "image": (r.get("image") or "").strip(),
                    "date": norm_date((r.get("date") or "").strip()),
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
    """Dated questions for that day first (Telegram /add with a date, or sheet 'date' column).
    If fewer than 5, the rest come from the rotation over the undated pool."""
    dated = extra_questions(qday) + [q for q in QUESTIONS if q.get("date") == qday]
    out = dated[:QUESTIONS_PER_DAY]
    if len(out) >= QUESTIONS_PER_DAY:
        return out
    pool = [q for q in QUESTIONS if not q.get("date")] + extra_questions(None)
    if not pool:
        return out
    d = date.fromisoformat(qday)
    idx = max(0, (d - START_DATE).days)
    n = len(pool)
    start = (idx * QUESTIONS_PER_DAY) % n
    i = 0
    while len(out) < QUESTIONS_PER_DAY and i < n:
        q = pool[(start + i) % n]
        if q not in out:
            out.append(q)
        i += 1
    return out


def qbyid(qid):
    for q in QUESTIONS:
        if q["id"] == qid:
            return q
    with db() as con:
        r = con.execute("SELECT * FROM extra_questions WHERE id=?", (qid,)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "category": r["category"] or "", "question": r["question"],
            "options": [r["option_a"], r["option_b"], r["option_c"], r["option_d"]],
            "correct": r["correct"], "explanation": r["explanation"] or "", "source": "",
            "image": r["image"] or "", "date": r["qdate"] or ""}


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
    if q.get("image"):
        try:
            img = q["image"]
            path = image_bytes(img) if (img.startswith("http") or "." in img) else None
            if path:
                with open(path, "rb") as fh:
                    await context.bot.send_photo(user_id, fh, caption=f"प्रश्न {idx + 1} साठी फोटो")
            else:
                await context.bot.send_photo(user_id, img, caption=f"प्रश्न {idx + 1} साठी फोटो")  # telegram file_id
        except Forbidden:
            return
        except Exception as e:
            log.warning("photo failed for q%s: %s", q["id"], e)
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
        "/top month महिन्याची यादी\n/remind off आठवण बंद\n/remind on आठवण सुरू"
        + ("\n\nऍडमिन\n/add नवा प्रश्न\n/bank प्रश्न साठा\n/show 12 प्रश्न बघा\n/del 12 प्रश्न काढा\n/preview आजचे प्रश्न\n/stats आकडे\n/post morning|evening|remind\n/broadcast मेसेज\n/export /backup" if is_admin(update) else ""))


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
    arg = context.args[0] if context.args else ""
    if arg in ("उद्या", "tomorrow"):
        qday = (date.fromisoformat(quiz_day()) + timedelta(days=1)).isoformat()
    else:
        qday = norm_date(arg) or quiz_day()
    qs = questions_for_day(qday)
    lines = [f"दिवस {day_number(qday)}  {day_label(qday)}"]
    for i, q in enumerate(qs, 1):
        tag = " (ठरलेला)" if q.get("date") == qday else ""
        tag += " (फोटो)" if q.get("image") else ""
        lines.append(f"\n{i}. {q['question']}{tag}\n   उत्तर {'अबकड'[q['correct']]}. {q['options'][q['correct']]}")
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
    if not doc:
        return
    name = (doc.file_name or "").lower()
    if not (name.endswith(".csv") or name.endswith(".xlsx")):
        return
    f = await doc.get_file()
    data = bytes(await f.download_as_bytearray())
    tmp = os.path.join(DATA_DIR, "upload_tmp" + (".xlsx" if name.endswith(".xlsx") else ".csv"))
    with open(tmp, "wb") as out:
        out.write(data)
    try:
        rows = read_rows(tmp)
        need = {"id", "question", "option_a", "option_b", "option_c", "option_d", "correct"}
        if not rows or not need.issubset(set(rows[0].keys())):
            await update.message.reply_text("फाइलमध्ये हे कॉलम हवेत: " + ", ".join(sorted(need)))
            return
    except Exception as e:
        await update.message.reply_text(f"फाइल वाचता आली नाही: {e}")
        return
    # always store as CSV
    with open(QUESTIONS_PATH, "w", encoding="utf-8", newline="") as out:
        cols = ["id", "category", "question", "option_a", "option_b", "option_c", "option_d",
                "correct", "explanation", "source", "image"]
        w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    load_questions()
    bad = len(rows) - len(QUESTIONS)
    await update.message.reply_text(
        f"प्रश्न साठा बदलला. आता {len(QUESTIONS)} प्रश्न." + (f" {bad} ओळी चुकीच्या म्हणून वगळल्या." if bad else ""))


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin photo: if inside /add flow, it becomes the question's image (caption = question text)."""
    if not is_admin(update):
        return
    st = context.user_data.get("add")
    if st is not None and st.get("step") == "question":
        st["image"] = update.message.photo[-1].file_id
        cap = (update.message.caption or "").strip()
        if cap:
            st["question"] = cap
            st["step"] = "a"
            await update.message.reply_text("फोटो घेतला. आता पर्याय अ लिहा.")
        else:
            await update.message.reply_text("फोटो घेतला. आता प्रश्न लिहा.")
        return
    await update.message.reply_text("प्रश्न टाकायचा असेल तर आधी /add पाठवा आणि मग फोटो.")


# --------------------------------------------------------------------------
# Admin: add questions directly in Telegram
# --------------------------------------------------------------------------
ADD_STEPS = ["question", "a", "b", "c", "d", "correct", "explanation", "confirm"]
ADD_PROMPTS = {
    "question": "प्रश्न लिहा. फोटो असेल तर फोटो पाठवा आणि कॅप्शनमध्ये प्रश्न.",
    "a": "पर्याय अ लिहा.",
    "b": "पर्याय ब लिहा.",
    "c": "पर्याय क लिहा.",
    "d": "पर्याय ड लिहा.",
    "correct": "बरोबर उत्तर कोणतं.",
    "explanation": "एका ओळीत स्पष्टीकरण लिहा. नको असेल तर /skip.",
}


def next_qid():
    return (max([q["id"] for q in QUESTIONS]) + 1) if QUESTIONS else 1


def append_question(q):
    cols = ["id", "category", "question", "option_a", "option_b", "option_c", "option_d",
            "correct", "explanation", "source", "image"]
    rows = read_rows(QUESTIONS_PATH) if os.path.exists(QUESTIONS_PATH) else []
    with open(QUESTIONS_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
        w.writerow({
            "id": q["id"], "category": q.get("category", ""), "question": q["question"],
            "option_a": q["options"][0], "option_b": q["options"][1],
            "option_c": q["options"][2], "option_d": q["options"][3],
            "correct": "ABCD"[q["correct"]], "explanation": q.get("explanation", ""),
            "source": q.get("source", ""), "image": q.get("image", ""),
        })
    load_questions()


def add_preview(st):
    o = st["options"]
    return (f"<b>प्रश्न {st['id']}</b>\n{st['question']}\n\n"
            f"अ. {o[0]}\nब. {o[1]}\nक. {o[2]}\nड. {o[3]}\n\n"
            f"बरोबर उत्तर <b>{'अबकड'[st['correct']]}</b>\n"
            + (f"स्पष्टीकरण {st['explanation']}\n" if st.get("explanation") else "")
            + ("फोटो आहे\n" if st.get("image") else ""))


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A human posts on the channel -> bot shows it to the admin with Send/Skip buttons.
    Only after the admin presses Send is it copied to every subscriber."""
    msg = update.channel_post
    if not msg or not CHANNEL_ID or not ADMIN_IDS:
        return
    chan = (msg.chat.username or "").lower()
    if CHANNEL_ID.lstrip("@").lower() != chan and str(msg.chat.id) != CHANNEL_ID:
        return
    if msg.from_user and msg.from_user.id == context.bot.id:
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("सगळ्यांना पाठवा", callback_data=f"cp_send_{msg.chat.id}_{msg.message_id}"),
        InlineKeyboardButton("नको", callback_data="cp_skip"),
    ]])
    for a in ADMIN_IDS:
        try:
            await context.bot.copy_message(a, msg.chat.id, msg.message_id)
            await context.bot.send_message(a, "ही चॅनलवरची पोस्ट सगळ्या सभासदांना खासगी पाठवायची का.", reply_markup=kb)
        except Exception as e:
            log.warning("admin preview failed %s: %s", a, e)


async def on_channel_post_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    if q.data == "cp_skip":
        await q.edit_message_text("ठीक. फक्त चॅनलवर राहिली.")
        return
    _, _, chat_id, message_id = q.data.split("_", 3)
    await q.edit_message_text("पाठवतोय.")
    with db() as con:
        users = con.execute("SELECT user_id FROM users WHERE remind=1").fetchall()
    n = 0
    for u in users:
        try:
            await context.bot.copy_message(u["user_id"], int(chat_id), int(message_id))
            n += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Forbidden:
            with db() as con:
                con.execute("UPDATE users SET remind=0 WHERE user_id=?", (u["user_id"],))
        except Exception as e:
            log.warning("copy to %s failed: %s", u["user_id"], e)
        await asyncio.sleep(0.05)
    await q.edit_message_text(f"चॅनलवरची पोस्ट {n} जणांना खासगी पाठवली.")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: pull the Google Sheet now."""
    if not is_admin(update):
        return
    if not SHEET_URL:
        await update.message.reply_text("SHEET_URL सेट केलेली नाही. .env मध्ये SHEET_URL टाका.")
        return
    r = await asyncio.to_thread(sync_sheet)
    if r == "changed":
        await update.message.reply_text(f"शीटमधून नवे प्रश्न घेतले. आता {len(QUESTIONS)} प्रश्न.")
    elif r == "same":
        await update.message.reply_text(f"शीटमध्ये बदल नाही. {len(QUESTIONS)} प्रश्न.")
    else:
        await update.message.reply_text(f"शीट वाचता आली नाही. {r}")


async def job_sync(context: ContextTypes.DEFAULT_TYPE):
    r = await asyncio.to_thread(sync_sheet)
    if r == "changed":
        log.info("sheet synced: %d questions", len(QUESTIONS))
        for a in ADMIN_IDS:
            await safe_send(context.bot, a, f"शीटमधले बदल घेतले. आता {len(QUESTIONS)} प्रश्न.")
    elif r.startswith("error"):
        log.warning("sheet sync %s", r)


def next_extra_id():
    with db() as con:
        r = con.execute("SELECT MAX(id) FROM extra_questions").fetchone()[0]
    return max(100000, (r or 0) + 1)


def save_extra(st, qdate):
    with db() as con:
        con.execute(
            "INSERT INTO extra_questions(id,category,question,option_a,option_b,option_c,option_d,correct,explanation,image,qdate,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (st["id"], st.get("category", ""), st["question"], *st["options"], st["correct"],
             st.get("explanation", ""), st.get("image", ""), qdate or "", now().isoformat()))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if SHEET_URL:
        await update.message.reply_text(
            "प्रश्न Google Sheet मधून येतात. शीटमध्ये नवी ओळ लिहा. ठराविक दिवशी हवा असेल तर date कॉलममध्ये तारीख. "
            "फोटो असेल तर image कॉलममध्ये Drive लिंक. दहा मिनिटांत बॉट घेतो किंवा /sync ने लगेच.\n" + SHEET_URL)
        return
    context.user_data["add"] = {"step": "question", "options": ["", "", "", ""], "image": "",
                                "explanation": "", "id": next_extra_id(),
                                "category": " ".join(context.args) if context.args else ""}
    await update.message.reply_text(
        "नवा प्रश्न. कधीही थांबायचं असेल तर /cancel.\n\n" + ADD_PROMPTS["question"])


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.pop("add", None) is not None:
        await update.message.reply_text("रद्द केलं.")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.user_data.get("add")
    if st and st["step"] == "explanation":
        st["explanation"] = ""
        st["step"] = "confirm"
        await send_add_confirm(update.message, st)


async def send_add_confirm(message, st):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("जतन करा", callback_data="add_save"),
                                InlineKeyboardButton("रद्द", callback_data="add_cancel")]])
    await message.reply_text(add_preview(st), parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text from admin while in /add flow."""
    st = context.user_data.get("add")
    if st is None or not is_admin(update):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    step = st["step"]
    if step == "question":
        st["question"] = text
        st["step"] = "a"
    elif step in ("a", "b", "c", "d"):
        st["options"]["abcd".index(step)] = text
        st["step"] = ADD_STEPS[ADD_STEPS.index(step) + 1]
    elif step == "correct":
        m = {"a": 0, "b": 1, "c": 2, "d": 3, "अ": 0, "ब": 1, "क": 2, "ड": 3}
        k = text.lower()[:1]
        if k not in m:
            await update.message.reply_text("फक्त अ ब क ड यापैकी एक अक्षर लिहा किंवा बटण दाबा.")
            return
        st["correct"] = m[k]
        st["step"] = "explanation"
    elif step == "explanation":
        st["explanation"] = text[:200]
        st["step"] = "confirm"
    elif step == "date":
        d = norm_date(text)
        if not d:
            await update.message.reply_text("तारीख समजली नाही. 2026-11-05 अशी लिहा.")
            return
        class _Q:  # small shim so finish_add can reuse edit/reply
            message = update.message
            async def edit_message_text(self, *a, **k):
                await update.message.reply_text(*a, **k)
        await finish_add(_Q(), context, st, d)
        return
    else:
        return
    step = st["step"]
    if step == "correct":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(x, callback_data=f"add_c_{i}") for i, x in enumerate("अबकड")]])
        await update.message.reply_text(ADD_PROMPTS["correct"], reply_markup=kb)
    elif step == "confirm":
        await send_add_confirm(update.message, st)
    else:
        await update.message.reply_text(ADD_PROMPTS[step])


async def on_add_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = context.user_data.get("add")
    data = q.data
    if st is None:
        await q.edit_message_text("हा प्रश्न आधीच संपला आहे. नवा प्रश्न /add ने.")
        return
    if data.startswith("add_c_"):
        st["correct"] = int(data[-1])
        st["step"] = "explanation"
        await q.edit_message_text(f"बरोबर उत्तर {'अबकड'[st['correct']]}.")
        await q.message.reply_text(ADD_PROMPTS["explanation"])
    elif data == "add_save":
        st["step"] = "when"
        tomorrow = (date.fromisoformat(quiz_day()) + timedelta(days=1)).isoformat()
        st["tomorrow"] = tomorrow
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"उद्या {day_label(tomorrow)}", callback_data="add_when_tomorrow")],
            [InlineKeyboardButton("ठराविक तारीख", callback_data="add_when_date")],
            [InlineKeyboardButton("साठ्यात शेवटी", callback_data="add_when_pool")],
        ])
        await q.edit_message_text(add_preview(st) + "\nहा प्रश्न कधी द्यायचा.", parse_mode=ParseMode.HTML, reply_markup=kb)
    elif data == "add_when_tomorrow":
        await finish_add(q, context, st, st["tomorrow"])
    elif data == "add_when_pool":
        await finish_add(q, context, st, "")
    elif data == "add_when_date":
        st["step"] = "date"
        await q.edit_message_text("तारीख लिहा. उदाहरण 2026-11-05 किंवा 05-11-2026.")
    elif data == "add_cancel":
        context.user_data.pop("add", None)
        await q.edit_message_text("रद्द केलं.")


async def finish_add(q, context, st, qdate):
    st["id"] = next_extra_id()
    save_extra(st, qdate)
    context.user_data.pop("add", None)
    if qdate:
        cnt = len(extra_questions(qdate)) + len([x for x in QUESTIONS if x.get("date") == qdate])
        msg = f"जतन झाला. {day_label(qdate)} साठी आता {cnt} प्रश्न ठरले आहेत."
        if cnt < QUESTIONS_PER_DAY:
            msg += f" उरलेले {QUESTIONS_PER_DAY - cnt} नेहमीच्या साठ्यातून येतील."
        elif cnt > QUESTIONS_PER_DAY:
            msg += f" पहिले {QUESTIONS_PER_DAY} जातील."
    else:
        msg = "जतन झाला. साठ्यात शेवटी जोडला."
    try:
        await q.edit_message_text(add_preview(st) + "\n" + msg, parse_mode=ParseMode.HTML)
    except BadRequest:
        await q.message.reply_text(msg)
    await q.message.reply_text("पुढचा प्रश्न /add. उद्याचे बघायचे असतील तर /preview " + (qdate or ""))


async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: how many questions, how many days they last, last 5 added."""
    if not is_admin(update):
        return
    pool = [q for q in QUESTIONS if not q.get("date")] + extra_questions(None)
    n = len(pool)
    today = date.fromisoformat(quiz_day())
    lines = [f"साठ्यात {n} प्रश्न. पुरतील {n // QUESTIONS_PER_DAY} दिवस.",
             f"शीटमधून {len(QUESTIONS)}. टेलिग्राममधून {len(extra_questions(None))}."]
    with db() as con:
        dated = con.execute(
            "SELECT qdate, COUNT(*) c FROM extra_questions WHERE qdate>=? GROUP BY qdate ORDER BY qdate LIMIT 10",
            (today.isoformat(),)).fetchall()
    sheet_dated = {}
    for q in QUESTIONS:
        if q.get("date") and q["date"] >= today.isoformat():
            sheet_dated[q["date"]] = sheet_dated.get(q["date"], 0) + 1
    days = {}
    for r in dated:
        days[r["qdate"]] = days.get(r["qdate"], 0) + r["c"]
    for k, v in sheet_dated.items():
        days[k] = days.get(k, 0) + v
    if days:
        lines.append("\nतारखेसाठी ठरलेले")
        for k in sorted(days)[:10]:
            lines.append(f"{day_label(k)}  {days[k]} प्रश्न")
    lines.append("\nशेवटचे पाच")
    for q in pool[-5:]:
        lines.append(f"{q['id']}. {q['question'][:60]}")
    lines.append("\n/add नवा प्रश्न. /del नंबर काढा. /preview तारीख त्या दिवसाचे बघा.")
    await update.message.reply_text("\n".join(lines))


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not context.args:
        return
    try:
        qid = int(context.args[0])
    except ValueError:
        return
    if qid >= 100000:
        with db() as con:
            con.execute("DELETE FROM extra_questions WHERE id=?", (qid,))
        await update.message.reply_text(f"प्रश्न {qid} काढला.")
        return
    if SHEET_URL:
        await update.message.reply_text("हा प्रश्न Google Sheet मधला आहे. शीटमधली ती ओळ काढा. दहा मिनिटांत बॉट घेतो किंवा /sync.")
        return
    rows = [r for r in read_rows(QUESTIONS_PATH) if str(r.get("id", "")).strip() != str(qid)]
    cols = ["id", "category", "question", "option_a", "option_b", "option_c", "option_d",
            "correct", "explanation", "source", "image"]
    with open(QUESTIONS_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    load_questions()
    await update.message.reply_text(f"प्रश्न {qid} काढला. आता {len(QUESTIONS)} प्रश्न.")


async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /show 12  shows question 12 in full."""
    if not is_admin(update) or not context.args:
        return
    try:
        q = qbyid(int(context.args[0]))
    except ValueError:
        q = None
    if not q:
        await update.message.reply_text("असा प्रश्न नाही.")
        return
    st = {"id": q["id"], "question": q["question"], "options": q["options"], "correct": q["correct"],
          "explanation": q["explanation"], "image": q["image"]}
    if q["image"]:
        try:
            await update.message.reply_photo(q["image"])
        except Exception:
            pass
    await update.message.reply_text(add_preview(st), parse_mode=ParseMode.HTML)


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
    if SHEET_URL:
        jq.run_repeating(job_sync, interval=SHEET_SYNC_MINUTES * 60, first=5, name="sheet_sync")
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
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.UpdateType.CHANNEL_POST, on_channel_post))
    app.add_handler(CallbackQueryHandler(on_channel_post_button, pattern="^cp_"))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("bank", cmd_bank))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CallbackQueryHandler(on_add_button, pattern="^add_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_add_text))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_play, pattern="^play$"))
    app.add_handler(CallbackQueryHandler(on_top_month, pattern="^top_month$"))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, on_document))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_photo))
    log.info("starting polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

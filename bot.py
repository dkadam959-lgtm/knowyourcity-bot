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
# one question from each of these five themes every day, in this order
CATEGORIES = ["वारसा", "पर्यावरण", "शहर", "पायाभूत सुविधा", "सामान्य ज्ञान"]
CAT_NAMES = {
    "वारसा": {"hi": "विरासत", "en": "Heritage"},
    "पर्यावरण": {"hi": "पर्यावरण", "en": "Environment"},
    "शहर": {"hi": "शहर", "en": "City"},
    "पायाभूत सुविधा": {"hi": "बुनियादी सुविधाएँ", "en": "Infrastructure"},
    "सामान्य ज्ञान": {"hi": "सामान्य ज्ञान", "en": "General Knowledge"},
}
LANGS = ("mr", "hi", "en")
LANG_NAMES = {"mr": "मराठी", "hi": "हिंदी", "en": "English"}
# optional sheet columns holding translations: question_hi, option_a_hi ... explanation_hi, same with _en
TR_FIELDS = ("question", "option_a", "option_b", "option_c", "option_d", "explanation")

T = {
"mr": {
 "choose_lang": "भाषा निवडा.",
 "lang_set": "भाषा मराठी. प्रश्न आणि मेसेज मराठीत येतील. /lang ने कधीही बदला.",
 "welcome": "नमस्कार {name}. <b>Know Your City</b> मध्ये स्वागत.\n\nरोज सकाळी {h} वाजता तुमच्या शहराबद्दल {n} प्रश्न. बरोबर उत्तराला {p} गुण आणि वेगाचा बोनस {b} पर्यंत. रोज आठवड्याला आणि महिन्याला विजेते जाहीर होतात.\n\n",
 "before_start": "सुरू करण्यापूर्वी चार छोट्या गोष्टी.",
 "press_play": "तयार असाल तर खालचं बटण दाबा.",
 "play_btn": "आजची स्पर्धा खेळा",
 "play_now_btn": "आता खेळा",
 "done_today": "आजची स्पर्धा तुम्ही पूर्ण केली आहे. उद्या सकाळी {h} वाजता पुढचे प्रश्न येतील.\n/score माझे गुण  /top आजची यादी",
 "done_short": "आजची स्पर्धा तुम्ही पूर्ण केली आहे. उद्या भेटू.",
 "closed": "आजची स्पर्धा रात्री {r} वाजता संपली. उद्या सकाळी {h} वाजता पुढचे प्रश्न.",
 "ask_name": "तुमचं नाव काय. हेच नाव विजेत्यांच्या यादीत दिसेल.",
 "ask_area": "तुम्ही कुठे राहता. भाग किंवा गाव. उदाहरण कोथरूड किंवा बारामती.",
 "ask_prof": "तुम्ही काय करता. बटण दाबा किंवा लिहा.",
 "ask_year": "तुमचं जन्मवर्ष. फक्त चार अंक. उदाहरण 1995.",
 "prof": ["विद्यार्थी", "नोकरी", "व्यवसाय", "शेती", "गृहिणी", "शिक्षक", "निवृत्त", "इतर"],
 "prof_set": "व्यवसाय {p}.",
 "year_err": "जन्मवर्ष चार अंकात लिहा. उदाहरण 1995.",
 "short_err": "किमान दोन अक्षरं लिहा.",
 "thanks_saved": "धन्यवाद. माहिती नोंदवली.",
 "thanks_play": "धन्यवाद. आता खालचं बटण दाबा.",
 "name_cur": "सध्याचं नाव {n}. ",
 "name_ask": "यादीत दाखवायचं नवं नाव लिहून पाठवा.",
 "name_err": "नाव किमान दोन अक्षरांचं हवं. पुन्हा लिहा.",
 "name_ok": "ठीक. यादीत {n} असं दिसेल.",
 "q_header": "प्रश्न {i}/{n}  ({s} सेकंद)",
 "photo_cap": "प्रश्न {i} साठी फोटो",
 "source": "स्रोत",
 "timeout": "वेळ संपली. हा प्रश्न ० गुण. पुढचा प्रश्न येतोय.",
 "referral_paid": "तुमच्या आमंत्रणाने आलेल्या मित्राने आजची स्पर्धा पूर्ण केली. तुम्हाला {b} बोनस गुण मिळाले.",
 "res_title": "<b>आजचा निकाल</b>  दिवस {d}",
 "res_pts": "गुण <b>{p}</b> / {m}",
 "res_time": "वेळ {s} सेकंद",
 "res_rank": "आजचा नंबर <b>{r}</b> ({t} खेळाडूंमध्ये)",
 "res_streak": "सलग दिवस <b>{n}</b>",
 "res_streak_bonus": "  सलग {n} दिवसांचा बोनस {b} गुण",
 "res_top": "आजचे आघाडीवर",
 "res_channel": "उत्तरं आणि विजेते रात्री {r} वाजता चॅनलवर.",
 "res_invite": "मित्राला आमंत्रण द्या आणि {b} बोनस गुण मिळवा.",
 "res_name": "यादीतलं तुमचं नाव बदलायचं असेल तर /name.",
 "share_btn": "मित्राला पाठवा",
 "share_text": "रोज 5 प्रश्न. तुमचं शहर किती ओळखता?",
 "month_btn": "माझा महिन्याचा हिशेब",
 "sc_title": "<b>माझे गुण</b>",
 "sc_today": "आज {p} गुण",
 "sc_rank": "  नंबर {r}",
 "sc_none": "  (अजून खेळला नाही)",
 "sc_week": "या आठवड्यात {t} (बोनस {b})",
 "sc_month": "या महिन्यात {t} (बोनस {b})",
 "sc_days": "एकूण खेळलेले दिवस {d}",
 "sc_streak": "सलग दिवस {n}",
 "board_none": "अजून कोणी खेळलं नाही.",
 "board_row": "{i}. {name}  {p} गुण  {s} से.",
 "top_day": "आजचे आघाडीवर", "top_week": "या आठवड्याचे आघाडीवर", "top_month": "या महिन्याचे आघाडीवर",
 "remind_off": "आठवण बंद केली. /remind on ने पुन्हा सुरू करता येईल.",
 "remind_on": "रोज दुपारी {h} वाजता न खेळल्यास आठवण येईल.",
 "help": "/play आजचे प्रश्न\n/score माझे गुण\n/top आजची यादी\n/top week आठवड्याची यादी\n/top month महिन्याची यादी\n/name यादीतलं नाव\n/lang भाषा\n/remind off आठवण बंद\n/remind on आठवण सुरू",
 "reminder": "आजची स्पर्धा अजून बाकी आहे. रात्री {r} पर्यंत {hl} तास उरले. दोन मिनिटांत {n} प्रश्न.",
 "morning": "सुप्रभात. {line}\nदिवस {d}. आजचे विषय {cats}.\nरात्री {r} पर्यंत खेळा.",
},
"hi": {
 "choose_lang": "भाषा चुनें.",
 "lang_set": "भाषा हिंदी. सवाल और संदेश हिंदी में आएँगे. /lang से कभी भी बदलें.",
 "welcome": "नमस्ते {name}. <b>Know Your City</b> में स्वागत है.\n\nरोज़ सुबह {h} बजे आपके शहर के बारे में {n} सवाल. सही जवाब पर {p} अंक और तेज़ी का बोनस {b} तक. रोज़ हफ़्ते और महीने के विजेता घोषित होते हैं.\n\n",
 "before_start": "शुरू करने से पहले चार छोटी बातें.",
 "press_play": "तैयार हों तो नीचे का बटन दबाएँ.",
 "play_btn": "आज की प्रतियोगिता खेलें",
 "play_now_btn": "अभी खेलें",
 "done_today": "आज की प्रतियोगिता आपने पूरी कर ली है. कल सुबह {h} बजे अगले सवाल आएँगे.\n/score मेरे अंक  /top आज की सूची",
 "done_short": "आज की प्रतियोगिता आपने पूरी कर ली है. कल मिलते हैं.",
 "closed": "आज की प्रतियोगिता रात {r} बजे खत्म हुई. कल सुबह {h} बजे अगले सवाल.",
 "ask_name": "आपका नाम क्या है. यही नाम विजेताओं की सूची में दिखेगा.",
 "ask_area": "आप कहाँ रहते हैं. इलाका या गाँव. उदाहरण कोथरूड या बारामती.",
 "ask_prof": "आप क्या करते हैं. बटन दबाएँ या लिखें.",
 "ask_year": "आपका जन्म वर्ष. सिर्फ़ चार अंक. उदाहरण 1995.",
 "prof": ["विद्यार्थी", "नौकरी", "व्यवसाय", "खेती", "गृहिणी", "शिक्षक", "सेवानिवृत्त", "अन्य"],
 "prof_set": "व्यवसाय {p}.",
 "year_err": "जन्म वर्ष चार अंकों में लिखें. उदाहरण 1995.",
 "short_err": "कम से कम दो अक्षर लिखें.",
 "thanks_saved": "धन्यवाद. जानकारी दर्ज हो गई.",
 "thanks_play": "धन्यवाद. अब नीचे का बटन दबाएँ.",
 "name_cur": "अभी का नाम {n}. ",
 "name_ask": "सूची में दिखाने का नया नाम लिखकर भेजें.",
 "name_err": "नाम कम से कम दो अक्षर का हो. फिर से लिखें.",
 "name_ok": "ठीक. सूची में {n} दिखेगा.",
 "q_header": "सवाल {i}/{n}  ({s} सेकंड)",
 "photo_cap": "सवाल {i} के लिए फ़ोटो",
 "source": "स्रोत",
 "timeout": "समय खत्म. इस सवाल पर 0 अंक. अगला सवाल आ रहा है.",
 "referral_paid": "आपके निमंत्रण से आए दोस्त ने आज की प्रतियोगिता पूरी की. आपको {b} बोनस अंक मिले.",
 "res_title": "<b>आज का नतीजा</b>  दिन {d}",
 "res_pts": "अंक <b>{p}</b> / {m}",
 "res_time": "समय {s} सेकंड",
 "res_rank": "आज का स्थान <b>{r}</b> ({t} खिलाड़ियों में)",
 "res_streak": "लगातार दिन <b>{n}</b>",
 "res_streak_bonus": "  लगातार {n} दिनों का बोनस {b} अंक",
 "res_top": "आज के अग्रणी",
 "res_channel": "जवाब और विजेता रात {r} बजे चैनल पर.",
 "res_invite": "दोस्त को निमंत्रण दें और {b} बोनस अंक पाएँ.",
 "res_name": "सूची में अपना नाम बदलना हो तो /name.",
 "share_btn": "दोस्त को भेजें",
 "share_text": "रोज़ 5 सवाल. अपने शहर को कितना जानते हैं?",
 "month_btn": "मेरा महीने का हिसाब",
 "sc_title": "<b>मेरे अंक</b>",
 "sc_today": "आज {p} अंक",
 "sc_rank": "  स्थान {r}",
 "sc_none": "  (अभी खेला नहीं)",
 "sc_week": "इस हफ़्ते {t} (बोनस {b})",
 "sc_month": "इस महीने {t} (बोनस {b})",
 "sc_days": "कुल खेले दिन {d}",
 "sc_streak": "लगातार दिन {n}",
 "board_none": "अभी किसी ने नहीं खेला.",
 "board_row": "{i}. {name}  {p} अंक  {s} से.",
 "top_day": "आज के अग्रणी", "top_week": "इस हफ़्ते के अग्रणी", "top_month": "इस महीने के अग्रणी",
 "remind_off": "याद दिलाना बंद किया. /remind on से फिर शुरू कर सकते हैं.",
 "remind_on": "रोज़ दोपहर {h} बजे न खेलने पर याद दिलाया जाएगा.",
 "help": "/play आज के सवाल\n/score मेरे अंक\n/top आज की सूची\n/top week हफ़्ते की सूची\n/top month महीने की सूची\n/name सूची का नाम\n/lang भाषा\n/remind off याद दिलाना बंद\n/remind on याद दिलाना शुरू",
 "reminder": "आज की प्रतियोगिता अभी बाकी है. रात {r} बजे तक {hl} घंटे बचे हैं. दो मिनट में {n} सवाल.",
 "morning": "सुप्रभात. {line}\nदिन {d}. आज के विषय {cats}.\nरात {r} बजे तक खेलें.",
},
"en": {
 "choose_lang": "Choose your language.",
 "lang_set": "Language set to English. Questions and messages will be in English. Change anytime with /lang.",
 "welcome": "Hello {name}. Welcome to <b>Know Your City</b>.\n\nEvery morning at {h} you get {n} questions about your city. {p} points for a correct answer and up to {b} speed bonus. Daily weekly and monthly winners.\n\n",
 "before_start": "Four quick things before we start.",
 "press_play": "Press the button below when ready.",
 "play_btn": "Play today's quiz",
 "play_now_btn": "Play now",
 "done_today": "You have finished today's quiz. Next questions tomorrow at {h} am.\n/score my points  /top today's board",
 "done_short": "You have finished today's quiz. See you tomorrow.",
 "closed": "Today's quiz closed at {r}:00. Next questions tomorrow at {h} am.",
 "ask_name": "What is your name. This name will appear on the winners board.",
 "ask_area": "Where do you live. Area or village. For example Kothrud or Baramati.",
 "ask_prof": "What do you do. Press a button or type.",
 "ask_year": "Your birth year. Four digits only. For example 1995.",
 "prof": ["Student", "Job", "Business", "Farming", "Homemaker", "Teacher", "Retired", "Other"],
 "prof_set": "Profession {p}.",
 "year_err": "Please type the birth year as four digits. For example 1995.",
 "short_err": "Please type at least two letters.",
 "thanks_saved": "Thank you. Details saved.",
 "thanks_play": "Thank you. Now press the button below.",
 "name_cur": "Current name {n}. ",
 "name_ask": "Type the new name to show on the board.",
 "name_err": "Name needs at least two letters. Try again.",
 "name_ok": "Done. The board will show {n}.",
 "q_header": "Question {i}/{n}  ({s} seconds)",
 "photo_cap": "Photo for question {i}",
 "source": "Source",
 "timeout": "Time is up. 0 points for this question. Next question coming.",
 "referral_paid": "A friend you invited finished today's quiz. You earned {b} bonus points.",
 "res_title": "<b>Today's result</b>  Day {d}",
 "res_pts": "Points <b>{p}</b> / {m}",
 "res_time": "Time {s} seconds",
 "res_rank": "Today's rank <b>{r}</b> (of {t} players)",
 "res_streak": "Streak <b>{n}</b> days",
 "res_streak_bonus": "  {n} day streak bonus {b} points",
 "res_top": "Today's leaders",
 "res_channel": "Answers and winners on the channel at {r}:00.",
 "res_invite": "Invite a friend and earn {b} bonus points.",
 "res_name": "To change your name on the board use /name.",
 "share_btn": "Send to a friend",
 "share_text": "5 questions a day. How well do you know your city?",
 "month_btn": "My monthly tally",
 "sc_title": "<b>My points</b>",
 "sc_today": "Today {p} points",
 "sc_rank": "  rank {r}",
 "sc_none": "  (not played yet)",
 "sc_week": "This week {t} (bonus {b})",
 "sc_month": "This month {t} (bonus {b})",
 "sc_days": "Days played {d}",
 "sc_streak": "Streak {n} days",
 "board_none": "Nobody has played yet.",
 "board_row": "{i}. {name}  {p} pts  {s} s",
 "top_day": "Today's leaders", "top_week": "This week's leaders", "top_month": "This month's leaders",
 "remind_off": "Reminders off. Use /remind on to turn them back on.",
 "remind_on": "You will get a reminder at {h}:00 if you have not played.",
 "help": "/play today's questions\n/score my points\n/top today's board\n/top week weekly board\n/top month monthly board\n/name board name\n/lang language\n/remind off reminders off\n/remind on reminders on",
 "reminder": "Today's quiz is still open. {hl} hours left until {r}:00. {n} questions in two minutes.",
 "morning": "Good morning. {line}\nDay {d}. Today's themes {cats}.\nPlay before {r}:00.",
},
}
MORNING_LINES_TR = {
 "hi": ["सोमवार की शुरुआत शहर की पहचान से.", "मंगलवार. आज पाँच सवाल और तीस सेकंड की चुनौती.", "बुधवार. हफ़्ते का बीच. सूची में ऊपर जाने का मौका.",
        "गुरुवार. कल जो छूटा आज पूरा करें.", "शुक्रवार. हफ़्ते के विजेताओं की दौड़ रंग में है.", "शनिवार. आराम से खेलें पर घड़ी तीस सेकंड की ही.", "रविवार. आज रात हफ़्ते के विजेता घोषित."],
 "en": ["Start Monday by knowing your city.", "Tuesday. Five questions and a thirty second challenge.", "Wednesday. Midweek. A chance to climb the board.",
        "Thursday. Make up for what you missed yesterday.", "Friday. The race for weekly winners is heating up.", "Saturday. Relax and play but the clock is still thirty seconds.", "Sunday. Weekly winners announced tonight."],
}


def t(lang, key, **kw):
    d = T.get(lang) or T["mr"]
    v = d.get(key, T["mr"].get(key, key))
    return v.format(**kw) if kw and isinstance(v, str) else v


def cat_name(cat, lang):
    if lang == "mr":
        return cat
    return CAT_NAMES.get(cat, {}).get(lang, cat)


def qtext(q, lang):
    """(question, options, explanation) in the user's language, Marathi if no translation."""
    tr = q.get("tr", {}).get(lang) if lang != "mr" else None
    if tr:
        return tr["question"], tr["options"], tr["explanation"]
    return q["question"], q["options"], q["explanation"]
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
                bonus INTEGER DEFAULT 0,
                display_name TEXT
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
        for col in ("display_name TEXT", "area TEXT", "profession TEXT", "birth_year INTEGER", "lang TEXT"):
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS bonuses (
                user_id INTEGER, qday TEXT, kind TEXT, points INTEGER,
                PRIMARY KEY (user_id, qday, kind)
            );
            CREATE TABLE IF NOT EXISTS day_questions (
                qday TEXT, position INTEGER, qid INTEGER,
                PRIMARY KEY (qday, position)
            );
            CREATE TABLE IF NOT EXISTS results_sent (
                user_id INTEGER, qday TEXT, PRIMARY KEY (user_id, qday)
            );
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
                    if isinstance(v, datetime):
                        v = v.date().isoformat()
                    elif isinstance(v, date):
                        v = v.isoformat()
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
        bad = path + ".bad"
        if os.path.exists(bad):
            raise ValueError("image link failed earlier")
        if not os.path.exists(path):
            try:
                data = fetch_url(drive_direct(img), timeout=30)
                if data[:1] == b"<" or len(data) < 100:
                    raise ValueError("image link is not public")
            except Exception:
                open(bad, "w").close()
                raise
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
                    "tr": {},
                }
                for lg in ("hi", "en"):
                    vals = {f: (r.get(f"{f}_{lg}") or "").strip() for f in TR_FIELDS}
                    if vals["question"] and all(vals[f"option_{x}"] for x in "abcd"):
                        q["tr"][lg] = {"question": vals["question"],
                                       "options": [vals["option_a"], vals["option_b"], vals["option_c"], vals["option_d"]],
                                       "explanation": vals["explanation"]}
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
    """The day's 5 questions. Frozen the first time they are asked for, so a sheet
    change later in the day cannot shift the set. Cleared with /refreeze."""
    with db() as con:
        rows = con.execute("SELECT qid FROM day_questions WHERE qday=? ORDER BY position", (qday,)).fetchall()
    if rows:
        out = [qbyid(r["qid"]) for r in rows]
        out = [q for q in out if q]
        if len(out) == len(rows):
            return out
    out = compute_questions_for_day(qday)
    today = quiz_day()
    if out and qday <= today:   # freeze only today and past; future days stay live
        with db() as con:
            con.execute("DELETE FROM day_questions WHERE qday=?", (qday,))
            con.executemany("INSERT INTO day_questions(qday,position,qid) VALUES(?,?,?)",
                            [(qday, i, q["id"]) for i, q in enumerate(out)])
    return out


def compute_questions_for_day(qday: str):
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
    idx = (d - START_DATE).days          # negative before launch is fine: still rotates daily
    # frame: one question per theme, each theme rotating through its own list
    by_cat = {c: [q for q in pool if q["category"] == c] for c in CATEGORIES}
    if all(by_cat[c] for c in CATEGORIES):
        taken = {q["id"] for q in out}
        for c in CATEGORIES:
            if len(out) >= QUESTIONS_PER_DAY:
                break
            lst = by_cat[c]
            for k in range(len(lst)):
                q = lst[(idx + k) % len(lst)]
                if q["id"] not in taken:
                    out.append(q); taken.add(q["id"])
                    break
        if len(out) >= QUESTIONS_PER_DAY:
            return out
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
    """Kept for compatibility. Names are shown as plain text, never as a link to the account."""
    return (name or "सभासद").replace("<", "").replace(">", "")


def shown_name(row):
    """Display name chosen by the user, else their Telegram first name. Plain text, no link."""
    n = (row["display_name"] if "display_name" in row.keys() and row["display_name"] else row["name"]) or "सभासद"
    return n.replace("<", "").replace(">", "")[:30]


def get_display_name(user_id):
    with db() as con:
        r = con.execute("SELECT display_name FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r["display_name"] if r and r["display_name"] else ""


def ulang(user_id) -> str:
    with db() as con:
        r = con.execute("SELECT lang FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r["lang"] if r and r["lang"] in LANGS else "mr"


def play_kb(lang, key="play_btn"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, key), callback_data="play")]])


def leaderboard(period: str, limit=10, qday=None):
    """period: day | week | month. Returns list of (user_id, name, points, seconds)."""
    today = date.fromisoformat(qday or quiz_day())
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
            SELECT a.user_id, u.name, u.display_name,
                   SUM(a.points) + CASE WHEN ?='day' THEN 0 ELSE
                     COALESCE((SELECT SUM(b.points) FROM bonuses b WHERE b.user_id=a.user_id AND b.qday BETWEEN ? AND ?),0) END AS pts,
                   SUM(a.seconds) AS secs
            FROM answers a JOIN users u ON u.user_id=a.user_id
            WHERE a.qday BETWEEN ? AND ?
            GROUP BY a.user_id
            ORDER BY pts DESC, secs ASC
            LIMIT ?
            """,
            (period, lo.isoformat(), hi.isoformat(), lo.isoformat(), hi.isoformat(), limit),
        ).fetchall()
    return rows


def user_rank_today(user_id, qday=None):
    with db() as con:
        rows = con.execute(
            """
            SELECT user_id, SUM(points) pts, SUM(seconds) secs
            FROM answers WHERE qday=?
            GROUP BY user_id ORDER BY pts DESC, secs ASC
            """,
            (qday or quiz_day(),),
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
def quiz_open_now() -> bool:
    """Quiz runs from QUIZ_HOUR to RESULT_HOUR (answers go to the channel at RESULT_HOUR)."""
    h = now().hour
    return QUIZ_HOUR <= h < RESULT_HOUR


async def send_next_question(context: ContextTypes.DEFAULT_TYPE, user_id: int, qday: str = None):
    qday = qday or quiz_day()
    with db() as con:
        open_poll = con.execute(
            "SELECT 1 FROM polls WHERE user_id=? AND qday=? AND answered=0", (user_id, qday)).fetchone()
        sent_ids = [r["qid"] for r in con.execute(
            "SELECT qid FROM polls WHERE user_id=? AND qday=?", (user_id, qday)).fetchall()]
    if open_poll:
        return          # a question is already waiting for this user
    qs = questions_for_day(qday)
    remaining = [q for q in qs if q["id"] not in sent_ids]
    if not remaining:
        await send_result(context, user_id, qday)
        return
    lang = ulang(user_id)
    if qday == quiz_day() and not quiz_open_now():
        await safe_send(context.bot, user_id, t(lang, "closed", r=RESULT_HOUR, h=QUIZ_HOUR))
        return
    q = remaining[0]
    idx = len(sent_ids)
    qq, opts, expl = qtext(q, lang)
    if q["source"]:
        expl = (expl + "\n" + t(lang, "source") + " " + q["source"]).strip()
    if q.get("image"):
        try:
            img = q["image"]
            path = await asyncio.to_thread(image_bytes, img) if (img.startswith("http") or "." in img) else None
            if path:
                with open(path, "rb") as fh:
                    await context.bot.send_photo(user_id, fh, caption=t(lang, "photo_cap", i=idx + 1))
            else:
                await context.bot.send_photo(user_id, img, caption=t(lang, "photo_cap", i=idx + 1))  # telegram file_id
        except Forbidden:
            return
        except Exception as e:
            log.warning("photo failed for q%s: %s", q["id"], e)
    try:
        msg = await context.bot.send_poll(
            chat_id=user_id,
            question=t(lang, "q_header", i=idx + 1, n=len(qs), s=POLL_OPEN_SECONDS) + "\n" + qq,
            options=opts,
            type="quiz",
            correct_option_id=q["correct"],
            is_anonymous=False,
            explanation=expl[:200] if expl else None,
            open_period=POLL_OPEN_SECONDS,
        )
    except Forbidden:
        return
    except BadRequest as e:
        log.error("send_poll failed for q%s: %s", q["id"], e)
        with db() as con:   # skip this question for this user so they are not stuck
            con.execute("INSERT OR REPLACE INTO polls(poll_id,user_id,qday,qid,qindex,sent_ts,answered) VALUES(?,?,?,?,?,?,1)",
                        (f"bad_{user_id}_{q['id']}", user_id, qday, q["id"], idx, now().timestamp()))
            con.execute("INSERT OR REPLACE INTO answers(user_id,qday,qid,correct,seconds,points,ts) VALUES(?,?,?,?,?,?,?)",
                        (user_id, qday, q["id"], 0, 0, 0, now().isoformat()))
        for a in ADMIN_IDS:
            await safe_send(context.bot, a, f"प्रश्न {q['id']} टेलिग्रामने नाकारला. {e}. प्रश्न ३०० आणि पर्याय १०० अक्षरांच्या आत हवेत.")
        await send_next_question(context, user_id, qday)
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
    await safe_send(context.bot, d["user_id"], t(ulang(d["user_id"]), "timeout"))
    await send_next_question(context, d["user_id"], d["qday"])


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
    await maybe_pay_referral(context, user.id, p["qday"])
    await send_next_question(context, user.id, p["qday"])


def add_bonus(user_id, qday, kind, points) -> bool:
    """One bonus of each kind per user per day. Returns True if newly added."""
    with db() as con:
        cur = con.execute("INSERT OR IGNORE INTO bonuses(user_id,qday,kind,points) VALUES(?,?,?,?)",
                          (user_id, qday, kind, points))
        return cur.rowcount > 0


async def maybe_pay_referral(context, user_id, qday=None):
    qday = qday or quiz_day()
    with db() as con:
        u = con.execute("SELECT referred_by, referral_paid FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not u or not u["referred_by"] or u["referral_paid"]:
            return
        if answered_count(user_id, qday) < QUESTIONS_PER_DAY:
            return
        con.execute("UPDATE users SET referral_paid=1 WHERE user_id=?", (user_id,))
    add_bonus(u["referred_by"], qday, f"ref_{user_id}", REFERRAL_BONUS)
    await safe_send(context.bot, u["referred_by"], t(ulang(u["referred_by"]), "referral_paid", b=REFERRAL_BONUS))


async def send_result(context, user_id, qday=None):
    qday = qday or quiz_day()
    with db() as con:
        cur = con.execute("INSERT OR IGNORE INTO results_sent(user_id,qday) VALUES(?,?)", (user_id, qday))
        if cur.rowcount == 0:
            return      # result for this day already sent once
    lang = ulang(user_id)
    rank, pts, secs, total = user_rank_today(user_id, qday)
    top = leaderboard("day", 3, qday)
    streak = streak_days(user_id)
    streak_line = t(lang, "res_streak", n=streak)
    if streak and streak % STREAK_BONUS_DAYS == 0 and add_bonus(user_id, qday, "streak", STREAK_BONUS):
        streak_line += t(lang, "res_streak_bonus", n=streak, b=STREAK_BONUS)
    lines = [t(lang, "res_title", d=day_number(qday)),
             t(lang, "res_pts", p=pts, m=QUESTIONS_PER_DAY * (POINTS_CORRECT + SPEED_BONUS[0][1])),
             t(lang, "res_time", s=f"{secs:.0f}"),
             t(lang, "res_rank", r=rank, t=total),
             streak_line, ""]
    if top:
        lines.append(t(lang, "res_top"))
        for i, r in enumerate(top, 1):
            lines.append(f"{i}. {shown_name(r)}  {r['pts']}")
    lines += ["", t(lang, "res_channel", r=RESULT_HOUR),
              t(lang, "res_invite", b=REFERRAL_BONUS),
              t(lang, "res_name")]
    from urllib.parse import quote
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "share_btn"),
                              url=f"https://t.me/share/url?url=https://t.me/{context.bot.username}?start=ref_{user_id}&text={quote(t(lang, 'share_text'))}")],
        [InlineKeyboardButton(t(lang, "month_btn"), callback_data="top_month")],
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
    with db() as con:
        has_lang = con.execute("SELECT lang FROM users WHERE user_id=?", (user.id,)).fetchone()["lang"] in LANGS
    if not has_lang:
        await ask_language(update.message, first=True)
        return
    await continue_start(update.message, context, user.id, user.first_name)


LANG_KB = InlineKeyboardMarkup([[InlineKeyboardButton("मराठी", callback_data="lang_mr"),
                                 InlineKeyboardButton("हिंदी", callback_data="lang_hi"),
                                 InlineKeyboardButton("English", callback_data="lang_en")]])


async def ask_language(message, first=False):
    txt = "भाषा निवडा.  भाषा चुनें.  Choose your language." if first else "भाषा निवडा / भाषा चुनें / Choose language"
    await message.reply_text(txt, reply_markup=LANG_KB)


async def continue_start(message, context, user_id, first_name):
    lang = ulang(user_id)
    done = answered_count(user_id, quiz_day())
    if done >= QUESTIONS_PER_DAY:
        await message.reply_text(t(lang, "done_today", h=QUIZ_HOUR))
        return
    text = (t(lang, "welcome", name=first_name, h=QUIZ_HOUR, n=QUESTIONS_PER_DAY, p=POINTS_CORRECT, b=SPEED_BONUS[0][1])
            + (f"{SPONSOR_TEXT}\n\n" if SPONSOR_TEXT else ""))
    if not profile_complete(user_id):
        await message.reply_text(text + t(lang, "before_start"), parse_mode=ParseMode.HTML)
        await ask_next_profile_step(message, context, user_id)
        return
    await message.reply_text(text + t(lang, "press_play"), parse_mode=ParseMode.HTML, reply_markup=play_kb(lang))


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_language(update.message)


async def on_lang_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = q.data[len("lang_"):]
    if lang not in LANGS:
        return
    user = q.from_user
    with db() as con:
        exists = con.execute("SELECT 1 FROM users WHERE user_id=?", (user.id,)).fetchone()
        if not exists:
            con.execute("INSERT INTO users(user_id,name,username,joined,source) VALUES(?,?,?,?,?)",
                        (user.id, user.full_name, user.username, now().isoformat(), ""))
        con.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user.id))
    try:
        await q.edit_message_text(t(lang, "lang_set"))
    except BadRequest:
        pass
    if not profile_complete(user.id):
        await continue_start(q.message, context, user.id, user.first_name)


# --------------------------------------------------------------------------
# Onboarding profile: name, area, profession, birth year (asked once)
# --------------------------------------------------------------------------
PROFILE_STEPS = [("display_name", "ask_name"), ("area", "ask_area"), ("profession", "ask_prof"), ("birth_year", "ask_year")]


def profile_complete(user_id) -> bool:
    with db() as con:
        r = con.execute("SELECT display_name, area, profession, birth_year FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(r and r["display_name"] and r["area"] and r["profession"] and r["birth_year"])


def next_profile_field(user_id):
    with db() as con:
        r = con.execute("SELECT display_name, area, profession, birth_year FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        return PROFILE_STEPS[0]
    for field, prompt in PROFILE_STEPS:
        if not r[field]:
            return (field, prompt)
    return None


async def ask_next_profile_step(message, context, user_id):
    lang = ulang(user_id)
    step = next_profile_field(user_id)
    if step is None:
        context.user_data.pop("profile_field", None)
        if answered_count(user_id, quiz_day()) >= QUESTIONS_PER_DAY:
            await message.reply_text(t(lang, "thanks_saved"))
        else:
            await message.reply_text(t(lang, "thanks_play"), reply_markup=play_kb(lang))
        return
    field, key = step
    context.user_data["profile_field"] = field
    if field == "profession":
        names = t(lang, "prof")
        # stored value is always the Marathi label so the Excel stays uniform
        mr = T["mr"]["prof"]
        rows, row = [], []
        for i, nm in enumerate(names):
            row.append(InlineKeyboardButton(nm, callback_data=f"prof_{mr[i]}"))
            if len(row) == 2:
                rows.append(row); row = []
        await message.reply_text(t(lang, key), reply_markup=InlineKeyboardMarkup(rows))
    else:
        await message.reply_text(t(lang, key))


async def save_profile_value(update: Update, context, user_id, field, value, message):
    value = value.strip().replace("<", "").replace(">", "")
    if field == "birth_year":
        digits = "".join(ch for ch in value if ch.isdigit())
        digits = digits.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
        if len(digits) != 4 or not (1920 <= int(digits) <= now().year - 5):
            await message.reply_text(t(ulang(user_id), "year_err"))
            return False
        value = int(digits)
    else:
        if len(value) < 2 or value.startswith("/"):
            await message.reply_text(t(ulang(user_id), "short_err"))
            return False
        value = value[:40]
    with db() as con:
        con.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))
    return True


async def on_profession_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    prof = q.data[len("prof_"):]
    with db() as con:
        con.execute("UPDATE users SET profession=? WHERE user_id=?", (prof, q.from_user.id))
    lang = ulang(q.from_user.id)
    shown = prof
    if prof in T["mr"]["prof"]:
        shown = t(lang, "prof")[T["mr"]["prof"].index(prof)]
    try:
        await q.edit_message_text(t(lang, "prof_set", p=shown))
    except BadRequest:
        pass
    await ask_next_profile_step(q.message, context, q.from_user.id)


async def cmd_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new = " ".join(context.args).strip() if context.args else ""
    if new:
        await set_display_name(update, context, new)
    else:
        context.user_data["await_name"] = True
        cur = get_display_name(update.effective_user.id)
        lang = ulang(update.effective_user.id)
        await update.message.reply_text((t(lang, "name_cur", n=cur) if cur else "") + t(lang, "name_ask"))


async def set_display_name(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str):
    name = name.strip().replace("<", "").replace(">", "")[:30]
    lang = ulang(update.effective_user.id)
    if len(name) < 2 or name.startswith("/"):
        await update.message.reply_text(t(lang, "name_err"))
        return
    with db() as con:
        con.execute("UPDATE users SET display_name=? WHERE user_id=?", (name, update.effective_user.id))
    context.user_data.pop("await_name", None)
    if answered_count(update.effective_user.id, quiz_day()) >= QUESTIONS_PER_DAY:
        await update.message.reply_text(t(lang, "name_ok", n=name))
    else:
        await update.message.reply_text(t(lang, "name_ok", n=name) + " " + t(lang, "press_play"), reply_markup=play_kb(lang))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text router: name entry first, then the admin /add flow."""
    if context.user_data.get("add") is not None:
        await on_add_text(update, context)
        return
    field = context.user_data.get("profile_field")
    if field:
        ok = await save_profile_value(update, context, update.effective_user.id, field, update.message.text or "", update.message)
        if ok:
            await ask_next_profile_step(update.message, context, update.effective_user.id)
        return
    if context.user_data.get("await_name"):
        await set_display_name(update, context, update.message.text or "")
        return


async def on_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    if ulang(user_id) not in LANGS or not profile_complete(user_id):
        with db() as con:
            r = con.execute("SELECT lang FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not r or r["lang"] not in LANGS:
            await ask_language(q.message, first=True)
        else:
            await ask_next_profile_step(q.message, context, user_id)
        return
    if answered_count(user_id, quiz_day()) >= QUESTIONS_PER_DAY:
        await q.edit_message_text(t(ulang(user_id), "done_short"))
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
    if not profile_complete(user_id):
        return await cmd_start(update, context)
    if answered_count(user_id, quiz_day()) >= QUESTIONS_PER_DAY:
        await update.message.reply_text(t(ulang(user_id), "done_short"))
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
        wkb = con.execute("SELECT COALESCE(SUM(points),0) FROM bonuses WHERE user_id=? AND qday>=?",
                          (user_id, (today - timedelta(days=today.weekday())).isoformat())).fetchone()[0]
        mob = con.execute("SELECT COALESCE(SUM(points),0) FROM bonuses WHERE user_id=? AND qday>=?",
                          (user_id, today.replace(day=1).isoformat())).fetchone()[0]
        days = con.execute("SELECT COUNT(DISTINCT qday) FROM answers WHERE user_id=?", (user_id,)).fetchone()[0]
    lang = ulang(user_id)
    text = (t(lang, "sc_title") + "\n"
            + t(lang, "sc_today", p=pts) + (t(lang, "sc_rank", r=rank) if rank else t(lang, "sc_none")) + "\n"
            + t(lang, "sc_week", t=wk + wkb, b=wkb) + "\n" + t(lang, "sc_month", t=mo + mob, b=mob) + "\n"
            + t(lang, "sc_days", d=days) + "\n" + t(lang, "sc_streak", n=streak_days(user_id)))
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def fmt_board(title, rows, lang="mr"):
    if not rows:
        return f"<b>{title}</b>\n" + t(lang, "board_none")
    lines = [f"<b>{title}</b>"]
    for i, r in enumerate(rows, 1):
        lines.append(t(lang, "board_row", i=i, name=shown_name(r), p=r["pts"], s=f"{r['secs']:.0f}"))
    return "\n".join(lines)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    period = (context.args[0] if context.args else "day").lower()
    if period not in ("day", "week", "month"):
        period = "day"
    lang = ulang(update.effective_user.id)
    await update.message.reply_text(fmt_board(t(lang, "top_" + period), leaderboard(period), lang), parse_mode=ParseMode.HTML)


async def on_top_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = ulang(q.from_user.id)
    await safe_send(context.bot, q.from_user.id, fmt_board(t(lang, "top_month"), leaderboard("month"), lang))


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    arg = (context.args[0] if context.args else "").lower()
    lang = ulang(user_id)
    if arg in ("off", "बंद"):
        val, msg = 0, t(lang, "remind_off")
    else:
        val, msg = 1, t(lang, "remind_on", h=REMIND_HOUR)
    with db() as con:
        con.execute("UPDATE users SET remind=? WHERE user_id=?", (val, user_id))
    await update.message.reply_text(msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        t(ulang(update.effective_user.id), "help")
        + ("\n\nऍडमिन\n/add नवा प्रश्न\n/bank प्रश्न साठा\n/show 12 प्रश्न बघा\n/del 12 प्रश्न काढा\n/preview आजचे प्रश्न\n/stats आकडे\n/post morning|evening|remind\n/broadcast मेसेज\n/export Excel\n/backup\n/refreeze आजचे प्रश्न पुन्हा" if is_admin(update) else ""))


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

    async def run():
        n = 0
        for u in users:
            if await safe_send(context.bot, u["user_id"], text):
                n += 1
            await asyncio.sleep(0.05)
        await safe_send(context.bot, update.effective_user.id, f"{n} जणांना पाठवलं.")
    context.application.create_task(run())
    await update.message.reply_text(f"{len(users)} जणांना पाठवतोय. झालं की सांगतो.")


def build_export_xlsx(path):
    """Excel with three sheets: users (profile), answers (one row per user per day, Y/N per question),
    and summary (per-category correct %)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active; ws.title = "users"
    ws.append(["user_id", "नाव", "टेलिग्राम नाव", "username", "भाषा", "भाग", "व्यवसाय", "जन्मवर्ष", "वय",
               "जोडले", "कुठून", "खेळलेले दिवस", "एकूण गुण"])
    with db() as con:
        rows = con.execute(
            """SELECT u.*, COUNT(DISTINCT a.qday) d, COALESCE(SUM(a.points),0) p
               FROM users u LEFT JOIN answers a ON a.user_id=u.user_id GROUP BY u.user_id ORDER BY u.joined""").fetchall()
        yr = now().year
        for r in rows:
            age = (yr - r["birth_year"]) if r["birth_year"] else ""
            ws.append([r["user_id"], r["display_name"] or "", r["name"] or "", r["username"] or "", r["lang"] or "mr",
                       r["area"] or "", r["profession"] or "", r["birth_year"] or "", age,
                       (r["joined"] or "")[:10], r["source"] or "", r["d"], r["p"]])
        ws2 = wb.create_sheet("answers")
        ws2.append(["तारीख", "user_id", "नाव", "भाग", "व्यवसाय", "जन्मवर्ष",
                    "प्र1 विषय", "प्र1", "प्र2 विषय", "प्र2", "प्र3 विषय", "प्र3", "प्र4 विषय", "प्र4", "प्र5 विषय", "प्र5",
                    "बरोबर", "गुण", "सेकंद"])
        days = [r["qday"] for r in con.execute("SELECT DISTINCT qday FROM answers ORDER BY qday").fetchall()]
        cat_stats = {}
        for qd in days:
            qs = questions_for_day(qd)
            order = [q["id"] for q in qs]
            cats = {q["id"]: q["category"] for q in qs}
            urows = con.execute(
                """SELECT a.user_id, u.display_name, u.area, u.profession, u.birth_year, a.qid, a.correct, a.points, a.seconds
                   FROM answers a JOIN users u ON u.user_id=a.user_id WHERE a.qday=? ORDER BY a.user_id""", (qd,)).fetchall()
            per = {}
            for a in urows:
                per.setdefault(a["user_id"], {"u": a, "ans": {}})
                per[a["user_id"]]["ans"][a["qid"]] = a
                c = cats.get(a["qid"], "")
                st = cat_stats.setdefault(c, [0, 0]); st[0] += a["correct"]; st[1] += 1
            for uid, v in per.items():
                u = v["u"]; line = [qd, uid, u["display_name"] or "", u["area"] or "", u["profession"] or "", u["birth_year"] or ""]
                corr = pts = secs = 0
                for qid in order:
                    a = v["ans"].get(qid)
                    line += [cats.get(qid, ""), ("Y" if a["correct"] else "N") if a else "-"]
                    if a:
                        corr += a["correct"]; pts += a["points"]; secs += a["seconds"]
                line += [corr, pts, round(secs)]
                ws2.append(line)
        ws3 = wb.create_sheet("summary")
        ws3.append(["विषय", "उत्तरं", "बरोबर", "बरोबर टक्के"])
        for c, (ok, tot) in cat_stats.items():
            ws3.append([c, tot, ok, round(100 * ok / tot) if tot else 0])
    for w in (ws, ws2, ws3):
        for cell in w[1]:
            cell.font = Font(bold=True, color="FFFFFF"); cell.fill = PatternFill("solid", fgColor="534AB7")
        w.freeze_panes = "A2"
    wb.save(path)


async def send_export_to(bot, chat_id):
    path = os.path.join(DATA_DIR, f"knowyourcity-{now():%Y-%m-%d}.xlsx")
    await asyncio.to_thread(build_export_xlsx, path)
    with open(path, "rb") as f:
        await bot.send_document(chat_id, f, filename=os.path.basename(path),
                                caption="सभासद. रोजची उत्तरं Y N. विषयानुसार आकडे.")


async def job_nightly_export(context: ContextTypes.DEFAULT_TYPE):
    for a in ADMIN_IDS:
        try:
            await send_export_to(context.bot, a)
        except Exception as e:
            log.warning("nightly export to %s failed: %s", a, e)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /export sends the Excel (users, answers Y/N, summary)."""
    if not is_admin(update):
        return
    await send_export_to(context.bot, update.effective_user.id)
    return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "username", "joined", "source", "days_played", "total_points"])
    with db() as con:
        rows = con.execute(
            """SELECT u.user_id, u.name, u.username, u.joined, u.source,
                      COUNT(DISTINCT a.qday) d,
                      COALESCE(SUM(a.points),0)+COALESCE((SELECT SUM(points) FROM bonuses b WHERE b.user_id=u.user_id),0) p
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
    if SHEET_URL:
        await update.message.reply_text("प्रश्न Google Sheet मधून येतात. फाइल पाठवण्याऐवजी शीटमध्ये लिहा.\n" + SHEET_URL)
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
                "correct", "explanation", "source", "image", "date"]
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
            "correct", "explanation", "source", "image", "date"]
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


OWN_CHANNEL_POSTS = set()


async def channel_send(bot, text, **kw):
    m = await bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML, **kw)
    OWN_CHANNEL_POSTS.add(m.message_id)
    return m


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A human posts on the channel -> bot shows it to the admin with Send/Skip buttons.
    Only after the admin presses Send is it copied to every subscriber."""
    msg = update.channel_post
    if not msg or not CHANNEL_ID or not ADMIN_IDS:
        return
    chan = (msg.chat.username or "").lower()
    if CHANNEL_ID.lstrip("@").lower() != chan and str(msg.chat.id) != CHANNEL_ID:
        return
    if msg.message_id in OWN_CHANNEL_POSTS or (msg.from_user and msg.from_user.id == context.bot.id):
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
    admin_id = q.from_user.id

    async def run():
        n = 0
        for u in users:
            for attempt in range(2):
                try:
                    await context.bot.copy_message(u["user_id"], int(chat_id), int(message_id))
                    n += 1
                    break
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Forbidden:
                    with db() as con:
                        con.execute("UPDATE users SET remind=0 WHERE user_id=?", (u["user_id"],))
                    break
                except Exception as e:
                    log.warning("copy to %s failed: %s", u["user_id"], e)
                    break
            await asyncio.sleep(0.05)
        await safe_send(context.bot, admin_id, f"चॅनलवरची पोस्ट {n} जणांना खासगी पाठवली.")
    context.application.create_task(run())


async def cmd_refreeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /refreeze [date]  recompute that day's 5 questions from the current bank."""
    if not is_admin(update):
        return
    qday = norm_date(context.args[0]) if context.args else quiz_day()
    qday = qday or quiz_day()
    with db() as con:
        con.execute("DELETE FROM day_questions WHERE qday=?", (qday,))
    qs = questions_for_day(qday)
    await update.message.reply_text(f"{day_label(qday)} चे प्रश्न पुन्हा ठरवले. " + ", ".join(str(q["id"]) for q in qs))


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
    context.user_data.pop("await_name", None)
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
            "correct", "explanation", "source", "image", "date"]
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
MORNING_LINES = [
    "सोमवारची सुरुवात शहराच्या ओळखीने.",
    "मंगळवार. आज पाच प्रश्न आणि तीस सेकंदांचं आव्हान.",
    "बुधवार. आठवड्याचा मध्य. यादीत वर जायची संधी.",
    "गुरुवार. काल चुकलं ते आज भरून काढा.",
    "शुक्रवार. आठवड्याच्या विजेत्यांची शर्यत आता रंगतेय.",
    "शनिवार. निवांत बसून खेळा पण घड्याळ तीस सेकंदांचंच.",
    "रविवार. आज रात्री आठवड्याचे विजेते जाहीर.",
]
MORNING_TAGS = [
    "आपल्या शहराबद्दल रोज एक नवी गोष्ट.",
    "पुणेकर म्हणून स्वतःला तपासा.",
    "जे शहर रोज बघता ते किती ओळखता.",
    "पाच प्रश्न. पाच विषय. एक शहर.",
    "वारसा पर्यावरण शहर सुविधा आणि सामान्य ज्ञान.",
    "मित्रांना आमंत्रण द्या आणि बोनस गुण मिळवा.",
    "सलग सात दिवस खेळा आणि दहा गुण बोनस.",
]


def morning_text(qday, cats):
    d = date.fromisoformat(qday)
    n = day_number(qday)
    head = MORNING_LINES[d.weekday()]
    tag = MORNING_TAGS[(n + d.weekday()) % len(MORNING_TAGS)]
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    lines = [f"<b>{head}</b>", f"दिवस {n}  {day_label(qday)}", ""]
    if cats:
        lines.append("आजचे विषय " + ". ".join(cats) + ".")
    lines.append(f"{QUESTIONS_PER_DAY} प्रश्न. प्रत्येकाला {POLL_OPEN_SECONDS} सेकंद. रात्री {RESULT_HOUR} पर्यंत खेळा.")
    lines.append(tag)
    if total >= 50:
        lines.append(f"आजपर्यंत {total} खेळाडू.")
    if SPONSOR_TEXT:
        lines += ["", SPONSOR_TEXT]
    return "\n".join(lines)


async def job_morning(context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_ID:
        return
    me = context.bot
    qday = quiz_day()
    cats = [q["category"] for q in questions_for_day(qday) if q["category"]]
    text = morning_text(qday, cats)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("आजची स्पर्धा खेळा", url=f"https://t.me/{me.username}?start=channel")]])
    try:
        await channel_send(context.bot, text, reply_markup=kb)
    except Exception as e:
        log.error("channel post failed: %s", e)


async def job_reminder(context: ContextTypes.DEFAULT_TYPE):
    qday = quiz_day()
    with db() as con:
        users = con.execute(
            "SELECT user_id, lang FROM users WHERE remind=1 AND user_id NOT IN "
            "(SELECT user_id FROM answers WHERE qday=?)", (qday,)).fetchall()
    hours_left = max(1, RESULT_HOUR - now().hour)
    n = 0
    for u in users:
        lg = u["lang"] if u["lang"] in LANGS else "mr"
        text = t(lg, "reminder", r=RESULT_HOUR, hl=hours_left, n=QUESTIONS_PER_DAY)
        if await safe_send(context.bot, u["user_id"], text, reply_markup=play_kb(lg, "play_now_btn")):
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
        en = q.get("tr", {}).get("en")
        if en:
            lines.append(f"   {en['question']}  ✔ {en['options'][q['correct']]}")
    with db() as con:
        played = con.execute("SELECT COUNT(DISTINCT user_id) FROM answers WHERE qday=?", (qday,)).fetchone()[0]
    lines.append(f"\nआज {played} जणांनी खेळलं.")
    lines.append("\n" + fmt_board("आजचे विजेते", leaderboard("day", 10)))
    if date.fromisoformat(qday).weekday() == 6:
        lines.append("\n" + fmt_board("या आठवड्याचे विजेते", leaderboard("week", 5)))
    if SPONSOR_TEXT:
        lines.append(f"\n{SPONSOR_TEXT}")
    try:
        await channel_send(context.bot, "\n".join(lines))
    except Exception as e:
        log.error("evening post failed: %s", e)


async def job_morning_push(context: ContextTypes.DEFAULT_TYPE):
    """Send the morning kick-off to every registered user (private chat)."""
    with db() as con:
        users = con.execute("SELECT user_id, lang FROM users WHERE remind=1").fetchall()
    qd = quiz_day()
    wd = date.fromisoformat(qd).weekday()
    cats = [q["category"] for q in questions_for_day(qd) if q["category"]]
    texts = {}
    for lg in LANGS:
        line = MORNING_LINES[wd] if lg == "mr" else MORNING_LINES_TR[lg][wd]
        texts[lg] = t(lg, "morning", line=line, d=day_number(qd), cats=". ".join(cat_name(c, lg) for c in cats), r=RESULT_HOUR)
    for u in users:
        lg = u["lang"] if u["lang"] in LANGS else "mr"
        await safe_send(context.bot, u["user_id"], texts[lg], reply_markup=play_kb(lg))
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("play", "आजचे प्रश्न / Play"),
        BotCommand("lang", "भाषा / Language"),
        BotCommand("score", "माझे गुण"),
        BotCommand("top", "आजची यादी"),
        BotCommand("name", "यादीतलं माझं नाव बदला"),
        BotCommand("remind", "आठवण सुरू किंवा बंद"),
        BotCommand("help", "मदत"),
    ])
    # polls left open across a restart: close them as 0 points so nobody is stuck
    with db() as con:
        stale = con.execute("SELECT poll_id,user_id,qday,qid FROM polls WHERE answered=0").fetchall()
        for r in stale:
            con.execute("UPDATE polls SET answered=1 WHERE poll_id=?", (r["poll_id"],))
            con.execute("INSERT OR IGNORE INTO answers(user_id,qday,qid,correct,seconds,points,ts) VALUES(?,?,?,?,?,?,?)",
                        (r["user_id"], r["qday"], r["qid"], 0, POLL_OPEN_SECONDS, 0, now().isoformat()))
    if stale:
        log.info("closed %d polls left open across restart", len(stale))
    jq = app.job_queue
    jq.run_daily(job_morning, time(QUIZ_HOUR, 0, tzinfo=TZ), name="morning")
    jq.run_daily(job_morning_push, time(QUIZ_HOUR, 1, tzinfo=TZ), name="morning_push")
    jq.run_daily(job_reminder, time(REMIND_HOUR, 0, tzinfo=TZ), name="reminder")
    jq.run_daily(job_evening, time(RESULT_HOUR, 0, tzinfo=TZ), name="evening")
    jq.run_daily(job_nightly_export, time(RESULT_HOUR, 30, tzinfo=TZ), name="nightly_export")
    if SHEET_URL:
        jq.run_repeating(job_sync, interval=SHEET_SYNC_MINUTES * 60, first=5, name="sheet_sync")
    log.info("jobs scheduled; questions=%d; channel=%s; admins=%s", len(QUESTIONS), CHANNEL_ID, ADMIN_IDS)


def main():
    init_db()
    load_questions()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()
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
    app.add_handler(CommandHandler("refreeze", cmd_refreeze))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("bank", cmd_bank))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CallbackQueryHandler(on_add_button, pattern="^add_"))
    app.add_handler(CommandHandler("name", cmd_name))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_play, pattern="^play$"))
    app.add_handler(CallbackQueryHandler(on_top_month, pattern="^top_month$"))
    app.add_handler(CallbackQueryHandler(on_profession_button, pattern="^prof_"))
    app.add_handler(CallbackQueryHandler(on_lang_button, pattern="^lang_"))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, on_document))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_photo))
    log.info("starting polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

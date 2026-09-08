import os
import sqlite3
import logging
import html
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_FILE = os.getenv("DB_FILE", "/data/together.db")
INACTIVITY_SECONDS = 180  # 3 րոպե чатում անգործության դեպքում փակելու ժամանակը
ACTIVE_DAYS = 7          # Discovery-ում հաշվի է առնվում վերջին 7 օրվա ակտիվությունը

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("Together")

# =========================================================
# STATES (ConversationHandler-ի համար)
# =========================================================

NAME, AGE, CITY, GENDER, LOOKING_FOR, ABOUT, PHOTO = range(7)

# =========================================================
# STORAGE / DB
# =========================================================

def ensure_storage():
    """Ստեղծում է DB-ի գրանցման թղթապանակը, եթե գոյություն չունի։"""
    folder = os.path.dirname(DB_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)

def db():
    """Ավարտած sqlite3 կապ. ակտիվացնում է foreign_keys և WAL ռեժիմը։"""
    ensure_storage()
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")     # Ակտիվացնում է foreign key սահմանափակումները
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")    # Ուղեցույց-ման-ի ռեժիմ՝ միաժամանակային ընթերցումների համար
    return conn

def init_db():
    """Սխեմայի ու աղյուսակների ստեղծում, եթե դրանք չեն ստեղծված."""
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            name TEXT,
            age INTEGER,
            city TEXT,
            gender TEXT,
            looking_for TEXT,
            about TEXT,
            photo_file_id TEXT,
            banned INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS swipes (
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (from_user, to_user)
        );

        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1 INTEGER NOT NULL,
            user2 INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user1, user2)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter INTEGER NOT NULL,
            reported INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT DEFAULT 'new',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS blocks (
            blocker INTEGER NOT NULL,
            blocked INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (blocker, blocked)
        );

        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT OR IGNORE INTO bot_settings(key, value)
        VALUES ('activity_notifications', '1');
        """)

# =========================================================
# HELPERS (օգնեքներ)
# =========================================================

def now():
    """Վերադարձնում է текղված UTC ժամանակ ISO ձեւաչափով."""
    return datetime.utcnow().isoformat(timespec="seconds")

def user_exists(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM users WHERE id=?", (user_id,)
        ).fetchone() is not None

def is_banned(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT banned FROM users WHERE id=?", (user_id,)
        ).fetchone()
        return bool(row and row["banned"])

def ensure_user(tg_user):
    """Աջակցության աղյուսակում գրանցում է օգտատիրոջ տվյալները (id, username) և last_active ժամանակը թարմացնում."""
    t = now()
    with db() as conn:
        conn.execute("""
            INSERT INTO users(id, username, created_at, last_active)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                last_active=excluded.last_active
        """, (tg_user.id, tg_user.username or "", t, t))

def touch(user_id):
    """Թարմացնում է օգտատիրոջ last_active ժամանակը՝ գործունեությունը հաշվող համար."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET last_active=? WHERE id=?",
            (now(), user_id)
        )

def log_activity(user_id, action):
    """Գրանցում է օգտատիրոջ արարքը activity_logs աղյուսակում."""
    with db() as conn:
        conn.execute(
            "INSERT INTO activity_logs(user_id, action, created_at) VALUES (?, ?, ?)",
            (user_id, action, now())
        )

def get_user(user_id):
    """Վերադարձնում է օգտատիրոջ ամբողջական տեղեկատվությունը DB-ից."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()

def update_user(user_id, **fields):
    """Թարմացնում է օգտատիրոջ տվյալների դաշտերը DB-ում."""
    if not fields:
        return
    fields["last_active"] = now()
    allowed = {
        "username", "name", "age", "city", "gender",
        "looking_for", "about", "photo_file_id", "banned"
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sql = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [user_id]
    with db() as conn:
        conn.execute(f"UPDATE users SET {sql} WHERE id=?", values)

def profile_complete(user_id):
    """Ստուգում է, թե արդյոք պրոֆիլը լրացվել է բոլոր դաշտերով։"""
    u = get_user(user_id)
    if not u:
        return False
    return all([
        u["name"], u["age"], u["city"], u["gender"],
        u["looking_for"], u["about"], u["photo_file_id"]
    ])

def activity_notifications_enabled():
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key='activity_notifications'"
        ).fetchone()
        return bool(row and row["value"] == "1")

def set_activity_notifications(enabled):
    with db() as conn:
        conn.execute("""
            INSERT INTO bot_settings(key, value) VALUES ('activity_notifications', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, ("1" if enabled else "0",))

async def notify_admin(context, text):
    """Ուղարկում է հաղորդագրություն ադմինին, եթե ակտիվ են հաղորդագրությունների ծանուցումները."""
    if ADMIN_ID and activity_notifications_enabled():
        try:
            await context.bot.send_message(ADMIN_ID, text)
        except Exception:
            log.exception("Admin notification failed")

# =========================================================
# KEYBOARDS (կոճակներ)
# =========================================================

def main_keyboard(user_id):
    """Գլխավոր մենյուի ստանդարտ կոճակներ."""
    rows = [
        ["👤 Իմ պրոֆիլը", "🔎 Գտնել մարդկանց"],
        ["❤️ Իմ Match-երը", "✏️ Խմբագրել պրոֆիլը"],
        ["⚙️ Կարգավորումներ"]
    ]
    if user_id == ADMIN_ID:
        rows.append(["🛡️ Admin մենյու"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def cancel_keyboard():
    return ReplyKeyboardMarkup(
        [["⬅️ Չեղարկել"]],
        resize_keyboard=True
    )

def gender_keyboard():
    return ReplyKeyboardMarkup(
        [["👨 Տղամարդ"], ["👩 Կին"]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def looking_keyboard():
    return ReplyKeyboardMarkup(
        [["👨 Տղամարդ"], ["👩 Կին"]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def report_keyboard(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Անպատշաճ բովանդակություն", callback_data=f"report:{user_id}:inappropriate")],
        [InlineKeyboardButton("👤 Կեղծ պրոֆիլ", callback_data=f"report:{user_id}:fake")],
        [InlineKeyboardButton("⚠️ Վիրավորանք / չարաշահում", callback_data=f"report:{user_id}:abuse")],
        [InlineKeyboardButton("📝 Այլ", callback_data=f"report:{user_id}:other")],
        [InlineKeyboardButton("🚫 Արգելափակել", callback_data=f"block:{user_id}")]
    ])

def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Ջնջել իմ պրոֆիլը", callback_data="delete_profile")],
        [InlineKeyboardButton("⬅️ Գլխավոր մենյու", callback_data="home")]
    ])

def admin_keyboard():
    status = "🟢 Միացված" if activity_notifications_enabled() else "🔴 Անջատված"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Վիճակագրություն", callback_data="admin:stats")],
        [InlineKeyboardButton("👥 Օգտատերեր", callback_data="admin:users")],
        [InlineKeyboardButton("🚨 Հաղորդումներ", callback_data="admin:reports")],
        [InlineKeyboardButton(f"🔔 Ակտիվության հաղորդագրություններ՝ {status}",
                              callback_data="admin:activity_toggle")],
        [InlineKeyboardButton("⬅️ Գլխավոր մենյու", callback_data="home")]
    ])

# =========================================================
# START / HOME
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    if is_banned(user.id):
        await update.message.reply_text("🚫 Ձեր պրոֆիլը արգելափակված է։")
        return ConversationHandler.END

    touch(user.id)
    context.user_data.clear()
    log_activity(user.id, "start")

    if profile_complete(user.id):
        # Եթե պրոֆիլը լրացված է, ուղղակի ցույց ենք տալիս գլխավոր մենյուն
        await home(update, context)
        return ConversationHandler.END

    await update.message.reply_text(
        "❤️ Բարի գալուստ Together։\n\n"
        "Այստեղ կարող եք ծանոթանալ նոր մարդկանց հետ։\n"
        "Սկսելու համար լրացրեք ձեր պրոֆիլը։",
        reply_markup=cancel_keyboard()
    )
    await update.message.reply_text("Ինչպե՞ս է ձեր անունը։")
    context.user_data["step"] = "name"
    return NAME

async def home(update, context):
    user_id = update.effective_user.id
    touch(user_id)
    context.user_data["mode"] = "home"
    text = (
        "❤️ <b>Together</b>\n\n"
        "Ընտրեք գործողությունը՝"
    )
    if update.callback_query:
        # Եթե callback (Inline կոճակից), փոփոխել նույն հաղորդագրությունը
        await update.callback_query.message.edit_text(text, parse_mode="HTML")
        await update.callback_query.message.reply_text(
            "Գլխավոր մենյու",
            reply_markup=main_keyboard(user_id)
        )
    else:
        await update.message.reply_text(
            text, parse_mode="HTML",
            reply_markup=main_keyboard(user_id)
        )

# =========================================================
# PROFILE CREATION / EDIT
# =========================================================

async def start_profile(update, context):
    context.user_data.clear()
    context.user_data["step"] = "name"
    await update.message.reply_text(
        "✏️ Սկսենք պրոֆիլի լրացումը։\n\nԻնչպե՞ս է ձեր անունը։",
        reply_markup=cancel_keyboard()
    )
    return NAME

async def edit_profile(update, context):
    context.user_data.clear()
    context.user_data["editing"] = True
    context.user_data["step"] = "name"
    await update.message.reply_text(
        "✏️ Փոխենք ձեր պրոֆիլը։\n\nԳրեք ձեր անունը։",
        reply_markup=cancel_keyboard()
    )
    return NAME

async def name_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    text = update.message.text.strip()
    if len(text) < 2 or len(text) > 40:
        await update.message.reply_text("❌ Անունը պետք է լինի 2–40 նիշ։")
        return NAME

    context.user_data["name"] = text
    context.user_data["step"] = "age"
    await update.message.reply_text("🎂 Քանի՞ տարեկան եք։", reply_markup=cancel_keyboard())
    return AGE

async def age_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    try:
        age = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Տարիքը գրեք թվով։ Օրինակ՝ 25")
        return AGE

    if not 18 <= age <= 99:
        await update.message.reply_text("❌ Տարիքը պետք է լինի 18–99։")
        return AGE

    context.user_data["age"] = age
    context.user_data["step"] = "city"
    await update.message.reply_text("📍 Ո՞ր քաղաքում եք ապրում։", reply_markup=cancel_keyboard())
    return CITY

async def city_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    city = update.message.text.strip()
    if len(city) < 2 or len(city) > 50:
        await update.message.reply_text("❌ Գրեք քաղաքի ճիշտ անվանումը։")
        return CITY

    context.user_data["city"] = city
    context.user_data["step"] = "gender"
    await update.message.reply_text(
        "⚧️ Ընտրեք ձեր սեռը։",
        reply_markup=gender_keyboard()
    )
    return GENDER

async def gender_step(update, context):
    text = update.message.text.strip()
    mapping = {"👨 Տղամարդ": "Տղամարդ", "👩 Կին": "Կին"}
    if text not in mapping:
        await update.message.reply_text("Խնդրում եմ ընտրեք տարբերակներից մեկը։", reply_markup=gender_keyboard())
        return GENDER

    context.user_data["gender"] = mapping[text]
    context.user_data["step"] = "looking_for"
    await update.message.reply_text(
        "❤️ Ո՞ւմ հետ եք ցանկանում ծանոթանալ։",
        reply_markup=looking_keyboard()
    )
    return LOOKING_FOR

async def looking_step(update, context):
    text = update.message.text.strip()
    mapping = {"👨 Տղամարդ": "Տղամարդ", "👩 Կին": "Կին"}
    if text not in mapping:
        await update.message.reply_text("Խնդրում եմ ընտրեք տարբերակներից մեկը։", reply_markup=looking_keyboard())
        return LOOKING_FOR

    context.user_data["looking_for"] = mapping[text]
    context.user_data["step"] = "about"
    await update.message.reply_text(
        "💬 Մի փոքր պատմեք ձեր մասին։\n\n"
        "Օրինակ՝ հետաքրքրություններ, զբաղմունք, ինչ եք փնտրում։",
        reply_markup=cancel_keyboard()
    )
    return ABOUT

async def about_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    about = update.message.text.strip()
    if len(about) < 5 or len(about) > 500:
        await update.message.reply_text("❌ Գրեք 5–500 նիշի սահմաններում։")
        return ABOUT

    context.user_data["about"] = about
    context.user_data["step"] = "photo"
    await update.message.reply_text(
        "📸 Ուղարկեք ձեր լուսանկարը։",
        reply_markup=cancel_keyboard()
    )
    return PHOTO

async def photo_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("❌ Խնդրում ենք ուղարկեք լուսանկար։")
        return PHOTO

    photo_id = update.message.photo[-1].file_id
    user_id = update.effective_user.id

    update_user(
        user_id,
        name=context.user_data["name"],
        age=context.user_data["age"],
        city=context.user_data["city"],
        gender=context.user_data["gender"],
        looking_for=context.user_data["looking_for"],
        about=context.user_data["about"],
        photo_file_id=photo_id
    )

    context.user_data.clear()
    log_activity(user_id, "profile_saved")
    await notify_admin(context, f"👤 Նոր/թարմացված պրոֆիլ՝ {user_id}")

    await update.message.reply_text(
        "✅ Ձեր պրոֆիլը պատրաստ է։\n\n"
        "Այժմ կարող եք գտնել մարդկանց և ծանոթանալ։",
        reply_markup=main_keyboard(user_id)
    )
    return ConversationHandler.END

# =========================================================
# PROFILE DISPLAY
# =========================================================

def profile_text(u):
    """Ստորագրում պրոֆիլի վերաբերյալ (Name, Age, City, Gender, Looking For, About). Պահանջվում է HTML.escape որոշ դաշտերում։"""
    return (
        f"👤 <b>{html.escape(u['name'])}</b>\n"
        f"🎂 {html.escape(str(u['age']))} տարեկան\n"
        f"📍 {html.escape(u['city'])}\n"
        f"⚧️ {html.escape(u['gender'])}\n"
        f"❤️ Փնտրում է՝ {html.escape(u['looking_for'])}\n\n"
        f"💬 {html.escape(u['about'])}"
    )

async def show_profile(update, context, user_id=None):
    """Ցուցադրել տվյալ օգտատիրոջ պրոֆիլը (ձևաչափված տեքստ + լուսանկար, եթե կա)։"""
    uid = user_id or update.effective_user.id
    u = get_user(uid)
    if not u:
        return

    text = profile_text(u)

    if update.callback_query:
        q = update.callback_query
        await q.answer()
        if u["photo_file_id"]:
            await q.message.reply_photo(
                u["photo_file_id"],
                caption=text,
                parse_mode="HTML"
            )
        else:
            await q.message.reply_text(text, parse_mode="HTML")
    else:
        if u["photo_file_id"]:
            await update.message.reply_photo(
                u["photo_file_id"],
                caption=text,
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(text, parse_mode="HTML")

# =========================================================
# DISCOVERY (Գտնել մարդկանց)
# =========================================================

def compatible(a, b):
    """Ստուգում է, արդյոք a օգտատերը և b օգտատերը համապատասխանում են միմյանց (gender և looking_for)։"""
    return (
        a["looking_for"] == b["gender"]
        and b["looking_for"] == a["gender"]
    )

def blocked_between(a, b):
    """Ստուգում է, եղե՞լ է արգելափակում երկու օգտատերերի միջև։"""
    with db() as conn:
        return conn.execute("""
            SELECT 1 FROM blocks
            WHERE (blocker=? AND blocked=?)
               OR (blocker=? AND blocked=?)
        """, (a, b, b, a)).fetchone() is not None

def next_candidate(user_id):
    """Գտնում է հաջորդ պատահական պրոֆիլի թեկնածու discovery-ի համար, հաշվի առնելով պայմանները։"""
    me = get_user(user_id)
    cutoff = (datetime.utcnow() - timedelta(days=ACTIVE_DAYS)).isoformat(timespec="seconds")

    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM users
            WHERE id != ?
              AND banned = 0
              AND last_active >= ?
              AND name IS NOT NULL
              AND age IS NOT NULL
              AND city IS NOT NULL
              AND gender IS NOT NULL
              AND looking_for IS NOT NULL
              AND about IS NOT NULL
              AND photo_file_id IS NOT NULL
              AND id NOT IN (
                  SELECT to_user FROM swipes WHERE from_user=?
              )
            ORDER BY RANDOM()
            LIMIT 50
        """, (user_id, cutoff, user_id)).fetchall()

    for candidate in rows:
        if compatible(me, candidate) and not blocked_between(user_id, candidate["id"]):
            return candidate
    return None

async def discover(update, context):
    user_id = update.effective_user.id

    if not profile_complete(user_id):
        await update.message.reply_text(
            "❗ Նախ լրացրեք ձեր պրոֆիլը։",
            reply_markup=main_keyboard(user_id)
        )
        return

    candidate = next_candidate(user_id)
    if not candidate:
        await update.message.reply_text(
            "🔎 Այս պահին համապատասխան նոր պրոֆիլ չգտնվեց։\n\n"
            "Փորձեք մի փոքր ուշ։"
        )
        return

    context.user_data["candidate_id"] = candidate["id"]
    context.user_data["mode"] = "discover"
    touch(user_id)

    text = profile_text(candidate)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❤️ Հավանել", callback_data=f"like:{candidate['id']}"),
            InlineKeyboardButton("🔥 Super Like", callback_data=f"super:{candidate['id']}")
        ],
        [
            InlineKeyboardButton("👎 Հաջորդը", callback_data=f"pass:{candidate['id']}")
        ],
        [
            InlineKeyboardButton("🚨 Հաղորդել", callback_data=f"report_menu:{candidate['id']}"),
            InlineKeyboardButton("🚫 Արգելափակել", callback_data=f"block:{candidate['id']}")
        ]
    ])

    if candidate["photo_file_id"]:
        await update.message.reply_photo(
            candidate["photo_file_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

async def swipe(update, context):
    """Կոնտակի հավանումը/չհավանումը/սուպերհավանումը մշակող ֆունկցիա (callback)."""
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    action, target_id = q.data.split(":")
    target_id = int(target_id)

    if action == "pass":
        action_db = "pass"
    elif action == "like":
        action_db = "like"
    else:
        action_db = "super"

    # Զանգվողը գրանցում ենք swipes աղյուսակում
    with db() as conn:
        conn.execute("""
            INSERT INTO swipes(from_user, to_user, action, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(from_user, to_user) DO UPDATE SET
                action=excluded.action,
                created_at=excluded.created_at
        """, (user_id, target_id, action_db, now()))

    log_activity(user_id, action_db)

    if action_db in ("like", "super"):
        # Ստուգում ենք, թե հակառակ կողմն էլ հավանել է արդյոք մեզ։
        with db() as conn:
            mutual = conn.execute("""
                SELECT action FROM swipes
                WHERE from_user=? AND to_user=?
                  AND action IN ('like', 'super')
            """, (target_id, user_id)).fetchone()

        if mutual:
            # Ստեղծվում է Match զույգ (user1 < user2 կարգով)
            u1, u2 = sorted([user_id, target_id])
            with db() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO matches(user1, user2, created_at)
                    VALUES (?, ?, ?)
                """, (u1, u2, now()))
                match = conn.execute(
                    "SELECT id FROM matches WHERE user1=? AND user2=?",
                    (u1, u2)
                ).fetchone()

            await q.message.edit_text(
                "🎉 <b>Match!</b>\n\n"
                "Դուք երկուսդ էլ հավանել եք միմյանց։ ❤️",
                parse_mode="HTML"
            )
            await q.message.reply_text(
                "💬 Կարող եք սկսել զրույցը։",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Բացել չատը", callback_data=f"chat:{match['id']}")],
                    [InlineKeyboardButton("🔎 Գտնել հաջորդին", callback_data="discover_next")]
                ])
            )

            # Մոտիվացնել դիմացինին նամակով՝ match ձեռք բերելու մասին
            try:
                await context.bot.send_message(
                    target_id,
                    "🎉 Դուք նոր Match ունեք։ ❤️\n"
                    "Բացեք Together-ը՝ զրույցը սկսելու համար։"
                )
            except Exception:
                pass
            return

        # Եթե դեռ չկան match, զգուշացնենք միայն առաջին հավանման դեպքում
        try:
            await context.bot.send_message(
                target_id,
                "❤️ Ինչ-որ մեկը հավանել է ձեր պրոֆիլը։\n"
                "Եթե փոխադարձ լինի, կունենաք Match։"
            )
        except Exception:
            pass

    # Եթե ոչ like/super, ապա սովորական էջափոխում
    await q.message.edit_text(
        "✅ Պահպանվեց։\n\nՍեղմեք «Հաջորդը»՝ նոր պրոֆիլ տեսնելու համար։"
    )
    await q.message.reply_text(
        "🔎 Շարունակե՞նք։",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ Հաջորդը", callback_data="discover_next")],
            [InlineKeyboardButton("🏠 Գլխավոր մենյու", callback_data="home")]
        ])
    )

# =========================================================
# MATCHES / CHAT
# =========================================================

def get_matches(user_id):
    """Վերադարձնում է օգտատիրոջ բոլոր matches ցուցակը (ամեն match-ի other_id-ն էլ հաշվարկված)."""
    with db() as conn:
        return conn.execute("""
            SELECT m.*,
                   CASE WHEN m.user1=? THEN m.user2 ELSE m.user1 END AS other_id
            FROM matches m
            WHERE m.user1=? OR m.user2=?
            ORDER BY m.created_at DESC
        """, (user_id, user_id, user_id)).fetchall()

def find_match(match_id, user_id):
    """Ստուգում է, թե match_id-ն պատկանո՞ւմ է տվյալ օգտատիրոջ. եթե ոչ, վերադարձնում None։"""
    with db() as conn:
        return conn.execute("""
            SELECT * FROM matches
            WHERE id=? AND (user1=? OR user2=?)
        """, (match_id, user_id, user_id)).fetchone()

async def show_matches(update, context):
    user_id = update.effective_user.id
    matches = get_matches(user_id)

    if not matches:
        await update.message.reply_text(
            "❤️ Դեռ Match չունեք։\n\nԳնացեք «🔎 Գտնել մարդկանց» բաժին։"
        )
        return

    buttons = []
    for m in matches:
        other = get_user(m["other_id"])
        if other:
            buttons.append([
                InlineKeyboardButton(
                    f"💬 {other['name']}",
                    callback_data=f"chat:{m['id']}"
                )
            ])

    await update.message.reply_text(
        "❤️ <b>Ձեր Match-երը</b>\n\nԸնտրեք զրույցը։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def open_chat(update, context):
    q = update.callback_query
    await q.answer()
    match_id = int(q.data.split(":")[1])
    user_id = q.from_user.id

    match = find_match(match_id, user_id)
    if not match:
        await q.message.reply_text("❌ Զրույցը հասանելի չէ։")
        return

    other_id = match["user2"] if match["user1"] == user_id else match["user1"]
    other = get_user(other_id)
    context.user_data["chat_match_id"] = match_id
    context.user_data["chat_other_id"] = other_id
    context.user_data["mode"] = "chat"
    context.user_data["last_chat_activity"] = datetime.utcnow().timestamp()

    await q.message.reply_text(
        f"💬 Դուք զրուցում եք <b>{other['name']}</b>-ի հետ։\n\n"
        "Գրեք հաղորդագրություն։\n"
        "Չատը 3 րոպե անգործությունից ավտոմատ կփակվի։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 Արգելափակել", callback_data=f"block:{other_id}")],
            [InlineKeyboardButton("🚨 Հաղորդել", callback_data=f"report_menu:{other_id}")],
            [InlineKeyboardButton("🏠 Գլխավոր մենյու", callback_data="home")]
        ])
    )

async def send_chat_message(update, context):
    """Ուղարկում է ոչ-կոմանդական տեքստային հաղորդագրություն զրույցի մյուս կողմին։"""
    user_id = update.effective_user.id
    match_id = context.user_data.get("chat_match_id")
    other_id = context.user_data.get("chat_other_id")

    if not match_id or not other_id:
        return False

    if blocked_between(user_id, other_id):
        await update.message.reply_text("🚫 Զրույցը հասանելի չէ։")
        context.user_data.clear()
        return True

    text = update.message.text.strip()
    if not text:
        return True

    context.user_data["last_chat_activity"] = datetime.utcnow().timestamp()

    with db() as conn:
        conn.execute("""
            INSERT INTO messages(match_id, sender_id, text, created_at)
            VALUES (?, ?, ?, ?)
        """, (match_id, user_id, text[:2000], now()))

    try:
        await context.bot.send_message(
            other_id,
            f"💬 Նոր հաղորդագրություն՝\n\n{text[:2000]}"
        )
    except Exception:
        pass

    await update.message.reply_text("✅ Ուղարկվեց։")
    return True

async def inactivity_cleanup(context):
    """30 վարկյանը մեկ ստուգում, թե չ՞աակտիվ 3 րոպե անցած չատերը. եթե այո՝ անջատում է դրանք:"""
    # Auto-closes chat/workflow state after INACTIVITY_SECONDS of inactivity.
    for chat_id, data in list(context.application.user_data.items()):
        last = data.get("last_chat_activity")
        if not last:
            continue
        if datetime.utcnow().timestamp() - last >= INACTIVITY_SECONDS:
            data.clear()
            try:
                await context.bot.send_message(
                    chat_id,
                    "⏱️ Չատը փակվեց 3 րոպե անգործությունից։\n\n"
                    "Ձեր Match-երը պահպանվել են։",
                    reply_markup=main_keyboard(chat_id)
                )
            except Exception:
                pass

# =========================================================
# REPORT / BLOCK
# =========================================================

async def report_menu(update, context):
    q = update.callback_query
    await q.answer()
    target = int(q.data.split(":")[1])
    await q.message.reply_text(
        "🚨 Ընտրեք հաղորդման պատճառը։",
        reply_markup=report_keyboard(target)
    )

async def report_user(update, context):
    q = update.callback_query
    await q.answer("Հաղորդումը ստացվեց։")
    _, target_id, reason = q.data.split(":")
    target_id = int(target_id)

    with db() as conn:
        conn.execute("""
            INSERT INTO reports(reporter, reported, reason, created_at)
            VALUES (?, ?, ?, ?)
        """, (q.from_user.id, target_id, reason, now()))

    log_activity(q.from_user.id, "report")
    await notify_admin(
        context,
        f"🚨 Նոր հաղորդում\n"
        f"Reporter: {q.from_user.id}\n"
        f"Reported: {target_id}\n"
        f"Պատճառ: {reason}"
    )
    await q.message.reply_text("✅ Հաղորդումը ուղարկվեց ադմինին։")

async def block_user(update, context):
    q = update.callback_query
    await q.answer("Օգտատերը արգելափակվեց։")
    target = int(q.data.split(":")[1])
    user_id = q.from_user.id

    if target == user_id:
        return

    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO blocks(blocker, blocked, created_at)
            VALUES (?, ?, ?)
        """, (user_id, target, now()))

    context.user_data.clear()
    log_activity(user_id, "block")

    await q.message.reply_text(
        "🚫 Օգտատերը արգելափակվեց։\n"
        "Նրա պրոֆիլը այլևս չի ցուցադրվի ձեզ։",
        reply_markup=main_keyboard(user_id)
    )

# =========================================================
# SETTINGS / DELETE PROFILE
# =========================================================

async def settings(update, context):
    await update.message.reply_text(
        "⚙️ <b>Կարգավորումներ</b>\n\n"
        "Այստեղ կարող եք կառավարել ձեր պրոֆիլը։",
        parse_mode="HTML",
        reply_markup=settings_keyboard()
    )

async def delete_confirm(update, context):
    q = update.callback_query
    await q.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Այո, ջնջել պրոֆիլը", callback_data="delete_yes")],
        [InlineKeyboardButton("⬅️ Չեղարկել", callback_data="home")]
    ])
    await q.message.reply_text(
        "⚠️ <b>Պրոֆիլի մշտական ջնջում</b>\n\n"
        "Ձեր պրոֆիլը, Match-երը, Like-երը և անձնական տվյալները "
        "կջնջվեն և գործողությունը հնարավոր չի լինի հետարկել։\n\n"
        "Շարունակե՞լ։",
        parse_mode="HTML",
        reply_markup=keyboard
    )

async def delete_profile(update, context):
    q = update.callback_query
    await q.answer("Պրոֆիլը ջնջվում է…")
    user_id = q.from_user.id

    with db() as conn:
        match_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM matches WHERE user1=? OR user2=?",
                (user_id, user_id)
            ).fetchall()
        ]

        if match_ids:
            placeholders = ",".join("?" * len(match_ids))
            conn.execute(
                f"DELETE FROM messages WHERE match_id IN ({placeholders})",
                match_ids
            )

        conn.execute(
            "DELETE FROM matches WHERE user1=? OR user2=?",
            (user_id, user_id)
        )
        conn.execute(
            "DELETE FROM swipes WHERE from_user=? OR to_user=?",
            (user_id, user_id)
        )
        conn.execute(
            "DELETE FROM blocks WHERE blocker=? OR blocked=?",
            (user_id, user_id)
        )
        conn.execute(
            "DELETE FROM reports WHERE reporter=? OR reported=?",
            (user_id, user_id)
        )
        conn.execute(
            "DELETE FROM activity_logs WHERE user_id=?",
            (user_id,)
        )
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    context.user_data.clear()

    await q.message.reply_text(
        "🗑️ Ձեր Together պրոֆիլը ամբողջությամբ ջնջվեց։\n\n"
        "Եթե ցանկանաք վերադառնալ, օգտագործեք /start։"
    )

# =========================================================
# ADMIN
# =========================================================

async def admin_menu(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "🛡️ <b>Admin մենյու</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )

async def admin_callback(update, context):
    q = update.callback_query
    await q.answer()

    if q.from_user.id != ADMIN_ID:
        return

    action = q.data.split(":")[1]

    if action == "activity_toggle":
        enabled = not activity_notifications_enabled()
        set_activity_notifications(enabled)
        log_activity(ADMIN_ID, "activity_notifications_toggle")
        await q.message.edit_reply_markup(reply_markup=admin_keyboard())
        return

    with db() as conn:
        if action == "stats":
            users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            active = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE last_active>=?",
                ((datetime.utcnow() - timedelta(days=7)).isoformat(timespec="seconds"),)
            ).fetchone()["c"]
            matches = conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"]
            messages = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
            reports = conn.execute("SELECT COUNT(*) c FROM reports WHERE status='new'").fetchone()["c"]

            text = (
                "📊 <b>Վիճակագրություն</b>\n\n"
                f"👥 Օգտատերեր՝ {users}\n"
                f"🟢 Ակտիվ՝ {active}\n"
                f"❤️ Match-եր՝ {matches}\n"
                f"💬 Հաղորդագրություններ՝ {messages}\n"
                f"🚨 Նոր հաղորդումներ՝ {reports}"
            )
            await q.message.reply_text(text, parse_mode="HTML")

        elif action == "users":
            rows = conn.execute(
                "SELECT id, name, city, banned FROM users ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            if not rows:
                await q.message.reply_text("Օգտատերեր չկան։")
                return
            lines = ["👥 <b>Վերջին օգտատերերը</b>\n"]
            for r in rows:
                status = "🚫" if r["banned"] else "🟢"
                lines.append(f"{status} {r['id']} — {r['name'] or 'Անուն չկա'} — {r['city'] or '-'}")
            await q.message.reply_text("\n".join(lines), parse_mode="HTML")

        elif action == "reports":
            rows = conn.execute("""
                SELECT reporter, reported, reason, created_at
                FROM reports
                WHERE status='new'
                ORDER BY created_at DESC
                LIMIT 20
            """).fetchall()
            if not rows:
                await q.message.reply_text("🚨 Նոր հաղորդումներ չկան։")
                return
            lines = ["🚨 <b>Հաղորդումներ</b>\n"]
            for r in rows:
                lines.append(
                    f"👤 {r['reporter']} → {r['reported']}\n"
                    f"📝 {r['reason']}\n"
                    f"🕒 {r['created_at']}"
                )
            await q.message.reply_text("\n\n".join(lines), parse_mode="HTML")

# =========================================================
# COMMANDS
# =========================================================

async def admin_command(update, context):
    await admin_menu(update, context)

async def cancel(update, context):
    context.user_data.clear()
    await home(update, context)
    return ConversationHandler.END

# =========================================================
# TEXT ROUTER
# =========================================================

async def text_router(update, context):
    """Համընդհանուր մուտք և չկոչվող կոճակների (text, photo) երթուղավորում (router)."""
    user_id = update.effective_user.id
    ensure_user(update.effective_user)

    if is_banned(user_id):
        await update.message.reply_text("🚫 Ձեր պրոֆիլը արգելափակված է։")
        return

    touch(user_id)

    # Եթե արդեն chat ռեժիմում ենք (message ուղարկելիս), նախ ստուգում ենք send_chat_message
    if context.user_data.get("chat_match_id"):
        handled = await send_chat_message(update, context)
        if handled:
            return

    text = update.message.text.strip()

    if text == "👤 Իմ պրոֆիլը":
        await show_profile(update, context)
    elif text == "🔎 Գտնել մարդկանց":
        await discover(update, context)
    elif text == "❤️ Իմ Match-երը":
        await show_matches(update, context)
    elif text == "✏️ Խմբագրել պրոֆիլը":
        await edit_profile(update, context)
    elif text == "⚙️ Կարգավորումներ":
        await settings(update, context)
    elif text == "🛡️ Admin մենյու" and user_id == ADMIN_ID:
        await admin_menu(update, context)
    else:
        await update.message.reply_text(
            "Խնդրում եմ ընտրեք գործողությունը կոճակներից։",
            reply_markup=main_keyboard(user_id)
        )

# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback_router(update, context):
    """Ուղղորդում է բոլոր CallbackQuery-ի տվյալները համապատասխան ֆունկցիաների համար."""
    q = update.callback_query
    data = q.data

    if data in ("like", "super", "pass"):
        return

    if data.startswith(("like:", "super:", "pass:")):
        await swipe(update, context)
    elif data == "discover_next":
        await q.answer()
        await q.message.reply_text("🔎 Փնտրում եմ…")
        # Discovery uses callback message context
        user_id = q.from_user.id
        candidate = next_candidate(user_id)
        if not candidate:
            await q.message.reply_text("🔎 Այս պահին նոր համապատասխան պրոֆիլ չկա։")
            return
        context.user_data["candidate_id"] = candidate["id"]
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❤️ Հավանել", callback_data=f"like:{candidate['id']}"),
                InlineKeyboardButton("🔥 Super Like", callback_data=f"super:{candidate['id']}")
            ],
            [InlineKeyboardButton("👎 Հաջորդը", callback_data=f"pass:{candidate['id']}")],
            [
                InlineKeyboardButton("🚨 Հաղորդել", callback_data=f"report_menu:{candidate['id']}"),
                InlineKeyboardButton("🚫 Արգելափակել", callback_data=f"block:{candidate['id']}")
            ]
        ])
        text = profile_text(candidate)
        if candidate["photo_file_id"]:
            await q.message.reply_photo(candidate["photo_file_id"], caption=text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    elif data.startswith("chat:"):
        await open_chat(update, context)
    elif data.startswith("report_menu:"):
        await report_menu(update, context)
    elif data.startswith("report:"):
        await report_user(update, context)
    elif data.startswith("block:"):
        await block_user(update, context)
    elif data == "delete_profile":
        await delete_confirm(update, context)
    elif data == "delete_yes":
        await delete_profile(update, context)
    elif data.startswith("admin:"):
        await admin_callback(update, context)
    elif data == "home":
        await q.answer()
        context.user_data.clear()
        await home(update, context)

# =========================================================
# ERROR HANDLING
# =========================================================

async def error_handler(update, context):
    """Գործարկել ստուգիչ, եթե որևէ անսպասելի սխալ տեղի է ունենում՝ այն ձերբակալելով, գրառմամբ և ադմինի ծանուցումով։"""
    log.exception("Unhandled error", exc_info=context.error)
    if ADMIN_ID:
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"❌ Together error:\n{type(context.error).__name__}: {context.error}"
            )
        except Exception:
            pass

# =========================================================
# MAIN ENTRYPOINT
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN միջավայրի փոփոխականը բացակայում է։")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^✏️ Խմբագրել պրոֆիլը$"), edit_profile),
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_step)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_step)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, city_step)],
            GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, gender_step)],
            LOOKING_FOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, looking_step)],
            ABOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, about_step)],
            PHOTO: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), photo_step)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^⬅️ Չեղարկել$"), cancel),
        ],
        allow_reentry=True,
    )

    app.add_handler(conversation)
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.PHOTO, text_router))
    app.add_error_handler(error_handler)

    # JobQueue: 30 վրկ քառակուսաբաժնում ստուգում (JobQueue-ի համար պետք է ծրագրում հաստատել APScheduler)
    app.job_queue.run_repeating(
        inactivity_cleanup,
        interval=30,
        first=30
    )

    log.info("Together bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

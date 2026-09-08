import os
import sqlite3
import logging
import html
from datetime import datetime, timedelta

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_FILE = os.getenv("DB_FILE", "/data/together.db")

# Չատը փակվում է 3 րոպե լրիվ անգործությունից հետո
INACTIVITY_SECONDS = 180
ACTIVE_DAYS = 7

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("Together")

# =========================================================
# STATES
# =========================================================

NAME, AGE, CITY, GENDER, LOOKING_FOR, ABOUT, PHOTO = range(7)

# =========================================================
# DATABASE
# =========================================================

def ensure_storage():
    folder = os.path.dirname(DB_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)


def db():
    ensure_storage()
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
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


def now():
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
    with db() as conn:
        conn.execute(
            "UPDATE users SET last_active=? WHERE id=?",
            (now(), user_id),
        )


def log_activity(user_id, action):
    with db() as conn:
        conn.execute(
            "INSERT INTO activity_logs(user_id, action, created_at) VALUES (?, ?, ?)",
            (user_id, action, now()),
        )


def get_user(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()


def update_user(user_id, **fields):
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
    u = get_user(user_id)
    if not u:
        return False

    return all([
        u["name"],
        u["age"],
        u["city"],
        u["gender"],
        u["looking_for"],
        u["about"],
        u["photo_file_id"],
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
            INSERT INTO bot_settings(key, value)
            VALUES ('activity_notifications', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, ("1" if enabled else "0",))


async def notify_admin(context, text):
    if ADMIN_ID and activity_notifications_enabled():
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=text,
                parse_mode="HTML",
            )
        except Exception:
            log.exception("Admin notification failed")


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard(user_id):
    rows = [
        ["👤 Իմ պրոֆիլը", "🔎 Գտնել մարդկանց"],
        ["❤️ Իմ Match-երը", "✏️ Խմբագրել պրոֆիլը"],
        ["🚫 Բլոկավորվածներ", "⚙️ Կարգավորումներ"],
    ]

    if user_id == ADMIN_ID:
        rows.append(["🛡️ Admin մենյու"])

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        input_field_placeholder="Ընտրեք գործողությունը…",
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        [["⬅️ Չեղարկել"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def gender_keyboard():
    return ReplyKeyboardMarkup(
        [["👨 Տղամարդ", "👩 Կին"], ["⬅️ Չեղարկել"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def looking_keyboard():
    return ReplyKeyboardMarkup(
        [["👨 Տղամարդ", "👩 Կին"], ["⬅️ Չեղարկել"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Գլխավոր մենյու", callback_data="home")]
    ])


def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Բլոկավորվածներ", callback_data="blocked_list")],
        [InlineKeyboardButton("🗑️ Ջնջել իմ պրոֆիլը", callback_data="delete_profile")],
        [InlineKeyboardButton("🏠 Գլխավոր մենյու", callback_data="home")],
    ])


def report_keyboard(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚫 Անպատշաճ բովանդակություն",
                callback_data=f"report:{user_id}:inappropriate",
            )
        ],
        [
            InlineKeyboardButton(
                "👤 Կեղծ պրոֆիլ",
                callback_data=f"report:{user_id}:fake",
            )
        ],
        [
            InlineKeyboardButton(
                "⚠️ Վիրավորանք / չարաշահում",
                callback_data=f"report:{user_id}:abuse",
            )
        ],
        [
            InlineKeyboardButton(
                "📝 Այլ",
                callback_data=f"report:{user_id}:other",
            )
        ],
        [
            InlineKeyboardButton(
                "🚫 Արգելափակել",
                callback_data=f"block:{user_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Փակել",
                callback_data="close_inline",
            )
        ],
    ])


def admin_keyboard():
    status = (
        "🟢 Միացված"
        if activity_notifications_enabled()
        else "🔴 Անջատված"
    )

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Վիճակագրություն", callback_data="admin:stats")],
        [InlineKeyboardButton("👥 Օգտատերեր", callback_data="admin:users")],
        [InlineKeyboardButton("🚨 Հաղորդումներ", callback_data="admin:reports")],
        [
            InlineKeyboardButton(
                f"🔔 Գործողությունների ծանուցումներ՝ {status}",
                callback_data="admin:activity_toggle",
            )
        ],
        [InlineKeyboardButton("🏠 Գլխավոր մենյու", callback_data="home")],
    ])


# =========================================================
# HOME
# =========================================================

async def home(update, context):
    user_id = update.effective_user.id
    touch(user_id)

    context.user_data["mode"] = "home"
    context.user_data.pop("chat_match_id", None)
    context.user_data.pop("chat_other_id", None)
    context.user_data.pop("chat_last_activity", None)

    text = (
        "❤️ <b>Together</b>\n\n"
        "Ծանոթացեք նոր մարդկանց, գտեք փոխադարձ համակրանք "
        "և սկսեք զրույց։\n\n"
        "👇 Ընտրեք գործողությունը՝"
    )

    if update.callback_query:
        q = update.callback_query
        try:
            await q.message.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

        await q.message.reply_text(
            "🏠 <b>Գլխավոր մենյու</b>",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    if is_banned(user.id):
        await update.message.reply_text(
            "🚫 Ձեր պրոֆիլը արգելափակված է։"
        )
        return ConversationHandler.END

    touch(user.id)
    context.user_data.clear()
    log_activity(user.id, "start")

    if profile_complete(user.id):
        await home(update, context)
        return ConversationHandler.END

    await update.message.reply_text(
        "❤️ <b>Բարի գալուստ Together</b>\n\n"
        "Այստեղ կարող եք ծանոթանալ նոր մարդկանց հետ։\n"
        "Սկսելու համար լրացրեք ձեր պրոֆիլը։",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await update.message.reply_text("👤 Ինչպե՞ս է ձեր անունը։")
    context.user_data["step"] = "name"
    return NAME


# =========================================================
# PROFILE CREATION / EDIT
# =========================================================

async def start_profile(update, context):
    context.user_data.clear()
    context.user_data["step"] = "name"

    await update.message.reply_text(
        "✏️ <b>Պրոֆիլի լրացում</b>\n\n"
        "👤 Գրեք ձեր անունը։",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    return NAME


async def edit_profile(update, context):
    context.user_data.clear()
    context.user_data["editing"] = True
    context.user_data["step"] = "name"

    await update.message.reply_text(
        "✏️ <b>Խմբագրել պրոֆիլը</b>\n\n"
        "👤 Գրեք ձեր անունը։",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    return NAME


async def name_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    text = update.message.text.strip()

    if len(text) < 2 or len(text) > 40:
        await update.message.reply_text(
            "❌ Անունը պետք է լինի 2–40 նիշ։"
        )
        return NAME

    context.user_data["name"] = text
    await update.message.reply_text(
        "🎂 Քանի՞ տարեկան եք։",
        reply_markup=cancel_keyboard(),
    )
    return AGE


async def age_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    try:
        age = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            "❌ Տարիքը գրեք թվով։ Օրինակ՝ 25"
        )
        return AGE

    if not 18 <= age <= 99:
        await update.message.reply_text(
            "❌ Տարիքը պետք է լինի 18–99։"
        )
        return AGE

    context.user_data["age"] = age

    await update.message.reply_text(
        "📍 Ո՞ր քաղաքում եք ապրում։",
        reply_markup=cancel_keyboard(),
    )
    return CITY


async def city_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    city = update.message.text.strip()

    if len(city) < 2 or len(city) > 50:
        await update.message.reply_text(
            "❌ Գրեք քաղաքի ճիշտ անվանումը։"
        )
        return CITY

    context.user_data["city"] = city

    await update.message.reply_text(
        "⚧️ <b>Ընտրեք ձեր սեռը</b>",
        parse_mode="HTML",
        reply_markup=gender_keyboard(),
    )
    return GENDER


async def gender_step(update, context):
    text = update.message.text.strip()

    if text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    mapping = {
        "👨 Տղամարդ": "Տղամարդ",
        "👩 Կին": "Կին",
    }

    if text not in mapping:
        await update.message.reply_text(
            "Խնդրում եմ ընտրեք կոճակներից։",
            reply_markup=gender_keyboard(),
        )
        return GENDER

    context.user_data["gender"] = mapping[text]

    await update.message.reply_text(
        "❤️ <b>Ո՞ւմ հետ եք ցանկանում ծանոթանալ</b>",
        parse_mode="HTML",
        reply_markup=looking_keyboard(),
    )
    return LOOKING_FOR


async def looking_step(update, context):
    text = update.message.text.strip()

    if text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    mapping = {
        "👨 Տղամարդ": "Տղամարդ",
        "👩 Կին": "Կին",
    }

    if text not in mapping:
        await update.message.reply_text(
            "Խնդրում եմ ընտրեք կոճակներից։",
            reply_markup=looking_keyboard(),
        )
        return LOOKING_FOR

    context.user_data["looking_for"] = mapping[text]

    await update.message.reply_text(
        "💬 <b>Մի փոքր պատմեք ձեր մասին</b>\n\n"
        "Օրինակ՝ հետաքրքրություններ, զբաղմունք, "
        "ինչ եք փնտրում։",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    return ABOUT


async def about_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    about = update.message.text.strip()

    if len(about) < 5 or len(about) > 500:
        await update.message.reply_text(
            "❌ Գրեք 5–500 նիշի սահմաններում։"
        )
        return ABOUT

    context.user_data["about"] = about

    await update.message.reply_text(
        "📸 <b>Ուղարկեք ձեր լուսանկարը</b>\n\n"
        "Լուսանկարը պարտադիր է։",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    return PHOTO


async def photo_step(update, context):
    if update.message.text == "⬅️ Չեղարկել":
        await home(update, context)
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text(
            "❌ Խնդրում ենք ուղարկել լուսանկար։"
        )
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
        photo_file_id=photo_id,
    )

    context.user_data.clear()
    log_activity(user_id, "profile_saved")

    await notify_admin(
        context,
        f"👤 <b>Նոր/թարմացված պրոֆիլ</b>\n"
        f"ID՝ <code>{user_id}</code>",
    )

    await update.message.reply_text(
        "✅ <b>Պրոֆիլը պատրաստ է։</b>\n\n"
        "Այժմ կարող եք գտնել մարդկանց և ծանոթանալ։",
        parse_mode="HTML",
        reply_markup=main_keyboard(user_id),
    )

    return ConversationHandler.END


# =========================================================
# PROFILE DISPLAY
# =========================================================

def profile_text(u):
    return (
        f"👤 <b>{html.escape(u['name'] or 'Անուն չկա')}</b>\n"
        f"🎂 {html.escape(str(u['age'] or '-'))} տարեկան\n"
        f"📍 {html.escape(u['city'] or '-')}\n"
        f"⚧️ {html.escape(u['gender'] or '-')}\n"
        f"❤️ Փնտրում է՝ {html.escape(u['looking_for'] or '-')}\n\n"
        f"💬 {html.escape(u['about'] or '')}"
    )


async def show_profile(update, context, user_id=None):
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
                photo=u["photo_file_id"],
                caption=text,
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )
        else:
            await q.message.reply_text(
                text,
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )
    else:
        if u["photo_file_id"]:
            await update.message.reply_photo(
                photo=u["photo_file_id"],
                caption=text,
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )
        else:
            await update.message.reply_text(
                text,
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )


# =========================================================
# DISCOVERY
# =========================================================

def compatible(a, b):
    return (
        a["looking_for"] == b["gender"]
        and b["looking_for"] == a["gender"]
    )


def blocked_between(a, b):
    with db() as conn:
        return conn.execute("""
            SELECT 1
            FROM blocks
            WHERE (blocker=? AND blocked=?)
               OR (blocker=? AND blocked=?)
        """, (a, b, b, a)).fetchone() is not None


def next_candidate(user_id):
    me = get_user(user_id)

    if not me:
        return None

    cutoff = (
        datetime.utcnow() - timedelta(days=ACTIVE_DAYS)
    ).isoformat(timespec="seconds")

    with db() as conn:
        rows = conn.execute("""
            SELECT *
            FROM users
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
                  SELECT to_user
                  FROM swipes
                  WHERE from_user=?
              )
            ORDER BY RANDOM()
            LIMIT 100
        """, (user_id, cutoff, user_id)).fetchall()

    for candidate in rows:
        if compatible(me, candidate) and not blocked_between(
            user_id, candidate["id"]
        ):
            return candidate

    return None


def discovery_keyboard(candidate_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❤️ Հավանել",
                callback_data=f"like:{candidate_id}",
            ),
            InlineKeyboardButton(
                "🔥 Super Like",
                callback_data=f"super:{candidate_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "👎 Հաջորդը",
                callback_data=f"pass:{candidate_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚨 Հաղորդել",
                callback_data=f"report_menu:{candidate_id}",
            ),
            InlineKeyboardButton(
                "🚫 Արգելափակել",
                callback_data=f"block:{candidate_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            )
        ],
    ])


async def send_candidate(message, candidate):
    text = profile_text(candidate)
    keyboard = discovery_keyboard(candidate["id"])

    if candidate["photo_file_id"]:
        await message.reply_photo(
            photo=candidate["photo_file_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def discover(update, context):
    user_id = update.effective_user.id

    if not profile_complete(user_id):
        await update.message.reply_text(
            "❗ Նախ լրացրեք ձեր պրոֆիլը։",
            reply_markup=main_keyboard(user_id),
        )
        return

    candidate = next_candidate(user_id)

    if not candidate:
        await update.message.reply_text(
            "🔎 <b>Այս պահին համապատասխան նոր պրոֆիլ չգտնվեց։</b>\n\n"
            "Փորձեք մի փոքր ուշ։",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
        return

    context.user_data["candidate_id"] = candidate["id"]
    context.user_data["mode"] = "discover"

    touch(user_id)
    await send_candidate(update.message, candidate)


async def discover_next(update, context):
    q = update.callback_query
    await q.answer("Փնտրում եմ…")

    user_id = q.from_user.id
    candidate = next_candidate(user_id)

    if not candidate:
        await q.message.reply_text(
            "🔎 Նոր համապատասխան պրոֆիլ այս պահին չկա։",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🏠 Գլխավոր",
                        callback_data="home",
                    )
                ]
            ]),
        )
        return

    context.user_data["candidate_id"] = candidate["id"]
    context.user_data["mode"] = "discover"
    touch(user_id)

    await send_candidate(q.message, candidate)


async def swipe(update, context):
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    action, target_id = q.data.split(":")
    target_id = int(target_id)

    if user_id == target_id or is_banned(user_id):
        return

    action_db = {
        "pass": "pass",
        "like": "like",
        "super": "super",
    }.get(action)

    if not action_db:
        return

    if blocked_between(user_id, target_id):
        await q.message.reply_text(
            "🚫 Այս պրոֆիլը հասանելի չէ։"
        )
        return

    with db() as conn:
        conn.execute("""
            INSERT INTO swipes(
                from_user, to_user, action, created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(from_user, to_user)
            DO UPDATE SET
                action=excluded.action,
                created_at=excluded.created_at
        """, (user_id, target_id, action_db, now()))

    touch(user_id)
    log_activity(user_id, action_db)

    if action_db in ("like", "super"):
        with db() as conn:
            mutual = conn.execute("""
                SELECT action
                FROM swipes
                WHERE from_user=?
                  AND to_user=?
                  AND action IN ('like', 'super')
            """, (target_id, user_id)).fetchone()

        if mutual:
            u1, u2 = sorted([user_id, target_id])

            with db() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO matches(
                        user1, user2, created_at
                    )
                    VALUES (?, ?, ?)
                """, (u1, u2, now()))

                match = conn.execute("""
                    SELECT id
                    FROM matches
                    WHERE user1=? AND user2=?
                """, (u1, u2)).fetchone()

            try:
                await q.message.edit_caption(
                    caption=(
                        "🎉 <b>Match!</b>\n\n"
                        "Դուք երկուսդ էլ հավանել եք միմյանց։ ❤️"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                try:
                    await q.message.edit_text(
                        "🎉 <b>Match!</b>\n\n"
                        "Դուք երկուսդ էլ հավանել եք միմյանց։ ❤️",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            await q.message.reply_text(
                "💬 <b>Կարող եք սկսել զրույցը։</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "💬 Բացել չատը",
                            callback_data=f"chat:{match['id']}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔎 Գտնել հաջորդին",
                            callback_data="discover_next",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏠 Գլխավոր",
                            callback_data="home",
                        )
                    ],
                ]),
            )

            try:
                await context.bot.send_message(
                    target_id,
                    "🎉 <b>Դուք նոր Match ունեք։ ❤️</b>\n\n"
                    "Բացեք Together-ը՝ զրույցը սկսելու համար։",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            return

        try:
            await context.bot.send_message(
                target_id,
                "❤️ Ինչ-որ մեկը հավանել է ձեր պրոֆիլը։\n\n"
                "Եթե փոխադարձ լինի, կունենաք Match։",
            )
        except Exception:
            pass

    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await q.message.reply_text(
        "✅ Պահպանվեց։",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "➡️ Հաջորդը",
                    callback_data="discover_next",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Գլխավոր",
                    callback_data="home",
                )
            ],
        ]),
    )


# =========================================================
# MATCHES / CHAT
# =========================================================

def get_matches(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT m.*,
                   CASE
                       WHEN m.user1=? THEN m.user2
                       ELSE m.user1
                   END AS other_id
            FROM matches m
            WHERE m.user1=? OR m.user2=?
            ORDER BY m.created_at DESC
        """, (user_id, user_id, user_id)).fetchall()


def find_match(match_id, user_id):
    with db() as conn:
        return conn.execute("""
            SELECT *
            FROM matches
            WHERE id=?
              AND (user1=? OR user2=?)
        """, (match_id, user_id, user_id)).fetchone()


async def show_matches(update, context):
    user_id = update.effective_user.id
    matches = get_matches(user_id)

    if not matches:
        await update.message.reply_text(
            "❤️ <b>Դեռ Match չունեք։</b>\n\n"
            "Գնացեք «🔎 Գտնել մարդկանց» բաժին։",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
        return

    buttons = []

    for m in matches:
        other = get_user(m["other_id"])

        if other:
            buttons.append([
                InlineKeyboardButton(
                    f"💬 {other['name'] or 'Օգտատեր'}",
                    callback_data=f"chat:{m['id']}",
                )
            ])

    buttons.append([
        InlineKeyboardButton(
            "🏠 Գլխավոր",
            callback_data="home",
        )
    ])

    await update.message.reply_text(
        "❤️ <b>Ձեր Match-երը</b>\n\n"
        "Ընտրեք զրույցը։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def open_chat(update, context):
    q = update.callback_query
    await q.answer()

    match_id = int(q.data.split(":")[1])
    user_id = q.from_user.id

    match = find_match(match_id, user_id)

    if not match:
        await q.message.reply_text(
            "❌ Զրույցը հասանելի չէ։"
        )
        return

    other_id = (
        match["user2"]
        if match["user1"] == user_id
        else match["user1"]
    )

    if blocked_between(user_id, other_id):
        await q.message.reply_text(
            "🚫 Զրույցը հասանելի չէ, քանի որ օգտատերերից մեկը արգելափակված է։",
            reply_markup=main_keyboard(user_id),
        )
        return

    other = get_user(other_id)

    context.user_data["chat_match_id"] = match_id
    context.user_data["chat_other_id"] = other_id
    context.user_data["mode"] = "chat"
    context.user_data["chat_last_activity"] = datetime.utcnow().timestamp()

    touch(user_id)

    await q.message.reply_text(
        f"💬 <b>{html.escape(other['name'] or 'Օգտատեր')}</b>\n\n"
        "Գրեք հաղորդագրություն։\n\n"
        "⏱️ <b>Չատը ավտոմատ կփակվի 3 րոպե լիակատար "
        "անգործությունից հետո։</b>\n"
        "Յուրաքանչյուր նոր հաղորդագրություն նորից սկսում է "
        "3 րոպեանոց ժամաչափը։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🚫 Արգելափակել",
                    callback_data=f"block:{other_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🚨 Հաղորդել",
                    callback_data=f"report_menu:{other_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Գլխավոր",
                    callback_data="home",
                )
            ],
        ]),
    )


async def send_chat_message(update, context):
    user_id = update.effective_user.id
    match_id = context.user_data.get("chat_match_id")
    other_id = context.user_data.get("chat_other_id")

    if not match_id or not other_id:
        return False

    if blocked_between(user_id, other_id):
        context.user_data.pop("chat_match_id", None)
        context.user_data.pop("chat_other_id", None)
        context.user_data.pop("chat_last_activity", None)

        await update.message.reply_text(
            "🚫 Զրույցը փակվեց, քանի որ օգտատերը արգելափակված է։",
            reply_markup=main_keyboard(user_id),
        )
        return True

    text = update.message.text.strip()

    if not text:
        return True

    # Ամեն հաղորդագրություն reset է անում inactivity timer-ը
    context.user_data["chat_last_activity"] = datetime.utcnow().timestamp()
    touch(user_id)
    log_activity(user_id, "chat_message")

    with db() as conn:
        conn.execute("""
            INSERT INTO messages(
                match_id, sender_id, text, created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            match_id,
            user_id,
            text[:2000],
            now(),
        ))

    try:
        await context.bot.send_message(
            other_id,
            f"💬 <b>Նոր հաղորդագրություն</b>\n\n"
            f"{html.escape(text[:2000])}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await update.message.reply_text("✅ Ուղարկվեց։")
    return True


async def inactivity_cleanup(context):
    """
    Յուրաքանչյուր 10 վայրկյանը մեկ ստուգում է բոլոր user_data-ները։
    Եթե chat_last_activity-ից անցել է 180 վրկ, չատային վիճակը մաքրվում է։
    """

    current = datetime.utcnow().timestamp()

    for chat_id, data in list(context.application.user_data.items()):
        last = data.get("chat_last_activity")

        if not last:
            continue

        if current - float(last) < INACTIVITY_SECONDS:
            continue

        match_id = data.get("chat_match_id")

        data.pop("chat_match_id", None)
        data.pop("chat_other_id", None)
        data.pop("chat_last_activity", None)
        data["mode"] = "home"

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏱️ <b>Չատը փակվեց</b>\n\n"
                    "3 րոպե շարունակ ոչ մի նոր հաղորդագրություն "
                    "չի ուղարկվել։\n\n"
                    "❤️ Ձեր Match-ը պահպանվել է։ Կարող եք ցանկացած "
                    "պահի նորից բացել զրույցը։"
                ),
                parse_mode="HTML",
                reply_markup=main_keyboard(chat_id),
            )
        except Exception:
            log.exception(
                "Could not send inactivity message to %s",
                chat_id,
            )

        if match_id:
            log_activity(chat_id, "chat_auto_closed")


# =========================================================
# BLOCKED USERS
# =========================================================

def get_blocked_users(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT u.*
            FROM blocks b
            JOIN users u ON u.id=b.blocked
            WHERE b.blocker=?
            ORDER BY b.created_at DESC
        """, (user_id,)).fetchall()


async def blocked_list(update, context):
    user_id = update.effective_user.id
    rows = get_blocked_users(user_id)

    if not rows:
        await update.message.reply_text(
            "🚫 <b>Բլոկավորվածներ</b>\n\n"
            "Դուք ոչ ոքի չեք արգելափակել։",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
        return

    buttons = []

    for u in rows:
        buttons.append([
            InlineKeyboardButton(
                f"🔓 Ապաբլոկավորել {u['name'] or u['id']}",
                callback_data=f"unblock:{u['id']}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🏠 Գլխավոր",
            callback_data="home",
        )
    ])

    await update.message.reply_text(
        "🚫 <b>Բլոկավորված պրոֆիլներ</b>\n\n"
        "Ընտրեք օգտատիրոջը՝ ապաբլոկավորելու համար։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def blocked_list_callback(update, context):
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    rows = get_blocked_users(user_id)

    if not rows:
        await q.message.reply_text(
            "🚫 Բլոկավորված պրոֆիլներ չկան։",
            reply_markup=main_keyboard(user_id),
        )
        return

    buttons = []

    for u in rows:
        buttons.append([
            InlineKeyboardButton(
                f"🔓 Ապաբլոկավորել {u['name'] or u['id']}",
                callback_data=f"unblock:{u['id']}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🏠 Գլխավոր",
            callback_data="home",
        )
    ])

    await q.message.reply_text(
        "🚫 <b>Բլոկավորված պրոֆիլներ</b>\n\n"
        "Սեղմեք «Ապաբլոկավորել»՝ օգտատիրոջը կրկին հասանելի դարձնելու համար։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def unblock_user(update, context):
    q = update.callback_query
    await q.answer("Ապաբլոկավորվեց։")

    user_id = q.from_user.id
    target_id = int(q.data.split(":")[1])

    with db() as conn:
        conn.execute("""
            DELETE FROM blocks
            WHERE blocker=? AND blocked=?
        """, (user_id, target_id))

    log_activity(user_id, "unblock")

    await q.message.reply_text(
        "🔓 <b>Օգտատերը ապաբլոկավորվեց։</b>\n\n"
        "Այժմ նրա պրոֆիլը կրկին կարող է հայտնվել ձեր Discovery-ում։",
        parse_mode="HTML",
        reply_markup=main_keyboard(user_id),
    )


# =========================================================
# REPORT / BLOCK
# =========================================================

async def report_menu(update, context):
    q = update.callback_query
    await q.answer()

    target = int(q.data.split(":")[1])

    await q.message.reply_text(
        "🚨 <b>Ընտրեք հաղորդման պատճառը</b>",
        parse_mode="HTML",
        reply_markup=report_keyboard(target),
    )


async def report_user(update, context):
    q = update.callback_query
    await q.answer("Հաղորդումը ստացվեց։")

    _, target_id, reason = q.data.split(":")
    target_id = int(target_id)

    if target_id == q.from_user.id:
        return

    with db() as conn:
        conn.execute("""
            INSERT INTO reports(
                reporter, reported, reason, created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            q.from_user.id,
            target_id,
            reason,
            now(),
        ))

    log_activity(q.from_user.id, "report")

    await notify_admin(
        context,
        f"🚨 <b>Նոր հաղորդում</b>\n\n"
        f"Reporter՝ <code>{q.from_user.id}</code>\n"
        f"Reported՝ <code>{target_id}</code>\n"
        f"Պատճառ՝ {html.escape(reason)}",
    )

    await q.message.reply_text(
        "✅ <b>Հաղորդումը ուղարկվեց ադմինին։</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(q.from_user.id),
    )


async def block_user(update, context):
    q = update.callback_query
    await q.answer("Օգտատերը արգելափակվեց։")

    target = int(q.data.split(":")[1])
    user_id = q.from_user.id

    if target == user_id:
        return

    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO blocks(
                blocker, blocked, created_at
            )
            VALUES (?, ?, ?)
        """, (user_id, target, now()))

    # Փակում ենք գործող chat-ը, եթե block-ը արվել է չատում
    if context.user_data.get("chat_other_id") == target:
        context.user_data.pop("chat_match_id", None)
        context.user_data.pop("chat_other_id", None)
        context.user_data.pop("chat_last_activity", None)

    context.user_data["mode"] = "home"

    log_activity(user_id, "block")

    await notify_admin(
        context,
        f"🚫 <b>Օգտատեր արգելափակվեց</b>\n\n"
        f"Blocker՝ <code>{user_id}</code>\n"
        f"Blocked՝ <code>{target}</code>",
    )

    await q.message.reply_text(
        "🚫 <b>Օգտատերը արգելափակվեց։</b>\n\n"
        "Նրա պրոֆիլը այլևս չի ցուցադրվի ձեզ։\n"
        "Ապաբլոկավորել կարող եք «🚫 Բլոկավորվածներ» բաժնից։",
        parse_mode="HTML",
        reply_markup=main_keyboard(user_id),
    )


# =========================================================
# SETTINGS / DELETE
# =========================================================

async def settings(update, context):
    await update.message.reply_text(
        "⚙️ <b>Կարգավորումներ</b>\n\n"
        "Կառավարեք ձեր պրոֆիլը և բլոկավորված օգտատերերին։",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


async def delete_confirm(update, context):
    q = update.callback_query
    await q.answer()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Այո, ջնջել պրոֆիլը",
                callback_data="delete_yes",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Չեղարկել",
                callback_data="home",
            )
        ],
    ])

    await q.message.reply_text(
        "⚠️ <b>Պրոֆիլի մշտական ջնջում</b>\n\n"
        "Ձեր պրոֆիլը, Match-երը, Like-երը և անձնական տվյալները "
        "կջնջվեն։ Գործողությունը հնարավոր չի լինի հետարկել։\n\n"
        "Շարունակե՞լ։",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def delete_profile(update, context):
    q = update.callback_query
    await q.answer("Պրոֆիլը ջնջվում է…")

    user_id = q.from_user.id

    with db() as conn:
        match_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM matches WHERE user1=? OR user2=?",
                (user_id, user_id),
            ).fetchall()
        ]

        if match_ids:
            placeholders = ",".join("?" * len(match_ids))
            conn.execute(
                f"DELETE FROM messages WHERE match_id IN ({placeholders})",
                match_ids,
            )

        conn.execute(
            "DELETE FROM matches WHERE user1=? OR user2=?",
            (user_id, user_id),
        )
        conn.execute(
            "DELETE FROM swipes WHERE from_user=? OR to_user=?",
            (user_id, user_id),
        )
        conn.execute(
            "DELETE FROM blocks WHERE blocker=? OR blocked=?",
            (user_id, user_id),
        )
        conn.execute(
            "DELETE FROM reports WHERE reporter=? OR reported=?",
            (user_id, user_id),
        )
        conn.execute(
            "DELETE FROM activity_logs WHERE user_id=?",
            (user_id,),
        )
        conn.execute(
            "DELETE FROM users WHERE id=?",
            (user_id,),
        )

    context.user_data.clear()

    await q.message.reply_text(
        "🗑️ <b>Ձեր Together պրոֆիլը ամբողջությամբ ջնջվեց։</b>\n\n"
        "Եթե ցանկանաք վերադառնալ, օգտագործեք /start։",
        parse_mode="HTML",
    )


# =========================================================
# ADMIN
# =========================================================

async def admin_menu(update, context):
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "🛡️ <b>Together Admin</b>\n\n"
        "Ընտրեք կառավարման բաժինը։",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
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

        await q.message.edit_reply_markup(
            reply_markup=admin_keyboard()
        )

        await q.message.reply_text(
            "🔔 Գործողությունների ծանուցումները՝ "
            + ("🟢 միացված են։" if enabled else "🔴 անջատված են։")
        )
        return

    with db() as conn:
        if action == "stats":
            users = conn.execute(
                "SELECT COUNT(*) c FROM users"
            ).fetchone()["c"]

            active = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE last_active>=?",
                (
                    (
                        datetime.utcnow()
                        - timedelta(days=7)
                    ).isoformat(timespec="seconds"),
                ),
            ).fetchone()["c"]

            matches = conn.execute(
                "SELECT COUNT(*) c FROM matches"
            ).fetchone()["c"]

            messages = conn.execute(
                "SELECT COUNT(*) c FROM messages"
            ).fetchone()["c"]

            reports = conn.execute(
                "SELECT COUNT(*) c FROM reports WHERE status='new'"
            ).fetchone()["c"]

            blocked = conn.execute(
                "SELECT COUNT(*) c FROM blocks"
            ).fetchone()["c"]

            banned = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE banned=1"
            ).fetchone()["c"]

            text = (
                "📊 <b>Վիճակագրություն</b>\n\n"
                f"👥 Օգտատերեր՝ <b>{users}</b>\n"
                f"🟢 Ակտիվ վերջին 7 օրում՝ <b>{active}</b>\n"
                f"❤️ Match-եր՝ <b>{matches}</b>\n"
                f"💬 Հաղորդագրություններ՝ <b>{messages}</b>\n"
                f"🚫 Բլոկավորումներ՝ <b>{blocked}</b>\n"
                f"🔨 Ban-վածներ՝ <b>{banned}</b>\n"
                f"🚨 Նոր հաղորդումներ՝ <b>{reports}</b>"
            )

            await q.message.reply_text(
                text,
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )

        elif action == "users":
            rows = conn.execute("""
                SELECT id, name, city, banned
                FROM users
                ORDER BY created_at DESC
                LIMIT 20
            """).fetchall()

            if not rows:
                await q.message.reply_text(
                    "👥 Օգտատերեր չկան։"
                )
                return

            lines = ["👥 <b>Վերջին օգտատերերը</b>\n"]

            for r in rows:
                status = "🔨" if r["banned"] else "🟢"
                lines.append(
                    f"{status} <code>{r['id']}</code> — "
                    f"{html.escape(r['name'] or 'Անուն չկա')} — "
                    f"{html.escape(r['city'] or '-')}"
                )

            await q.message.reply_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )

        elif action == "reports":
            rows = conn.execute("""
                SELECT reporter, reported, reason, created_at
                FROM reports
                WHERE status='new'
                ORDER BY created_at DESC
                LIMIT 20
            """).fetchall()

            if not rows:
                await q.message.reply_text(
                    "🚨 Նոր հաղորդումներ չկան։",
                    reply_markup=back_keyboard(),
                )
                return

            lines = ["🚨 <b>Նոր հաղորդումներ</b>\n"]

            for r in rows:
                lines.append(
                    f"👤 <code>{r['reporter']}</code> → "
                    f"<code>{r['reported']}</code>\n"
                    f"📝 {html.escape(r['reason'])}\n"
                    f"🕒 {html.escape(r['created_at'])}"
                )

            await q.message.reply_text(
                "\n\n".join(lines),
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )


# =========================================================
# COMMANDS / CANCEL
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
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    user_id = user.id

    ensure_user(user)

    if is_banned(user_id):
        await update.message.reply_text(
            "🚫 Ձեր պրոֆիլը արգելափակված է։"
        )
        return

    touch(user_id)

    # Չատում գտնվող user-ի ցանկացած TEXT գնում է chat message-ի մեջ
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
        # ConversationHandler-ը սովորաբար կբռնի սա,
        # բայց այստեղ էլ պահում ենք fallback-ը։
        await edit_profile(update, context)

    elif text == "🚫 Բլոկավորվածներ":
        await blocked_list(update, context)

    elif text == "⚙️ Կարգավորումներ":
        await settings(update, context)

    elif text == "🛡️ Admin մենյու" and user_id == ADMIN_ID:
        await admin_menu(update, context)

    else:
        await update.message.reply_text(
            "👇 Խնդրում եմ ընտրեք գործողությունը կոճակներից։",
            reply_markup=main_keyboard(user_id),
        )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback_router(update, context):
    q = update.callback_query
    data = q.data or ""

    if data.startswith(("like:", "super:", "pass:")):
        await swipe(update, context)

    elif data == "discover_next":
        await discover_next(update, context)

    elif data.startswith("chat:"):
        await open_chat(update, context)

    elif data.startswith("report_menu:"):
        await report_menu(update, context)

    elif data.startswith("report:"):
        await report_user(update, context)

    elif data.startswith("block:"):
        await block_user(update, context)

    elif data.startswith("unblock:"):
        await unblock_user(update, context)

    elif data == "blocked_list":
        await blocked_list_callback(update, context)

    elif data == "delete_profile":
        await delete_confirm(update, context)

    elif data == "delete_yes":
        await delete_profile(update, context)

    elif data.startswith("admin:"):
        await admin_callback(update, context)

    elif data == "close_inline":
        await q.answer()
        try:
            await q.message.delete()
        except Exception:
            pass

    elif data == "home":
        await q.answer()
        context.user_data.clear()
        await home(update, context)

    else:
        await q.answer()


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    log.exception(
        "Unhandled error",
        exc_info=context.error,
    )

    if ADMIN_ID:
        try:
            await context.bot.send_message(
                ADMIN_ID,
                "❌ <b>Together error</b>\n"
                f"<code>{html.escape(str(context.error))}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN միջավայրի փոփոխականը բացակայում է։"
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(
                filters.Regex("^✏️ Խմբագրել պրոֆիլը$"),
                edit_profile,
            ),
        ],
        states={
            NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    name_step,
                )
            ],
            AGE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    age_step,
                )
            ],
            CITY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    city_step,
                )
            ],
            GENDER: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    gender_step,
                )
            ],
            LOOKING_FOR: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    looking_step,
                )
            ],
            ABOUT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    about_step,
                )
            ],
            PHOTO: [
                MessageHandler(
                    filters.PHOTO
                    | (filters.TEXT & ~filters.COMMAND),
                    photo_step,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(
                filters.Regex("^⬅️ Չեղարկել$"),
                cancel,
            ),
        ],
        allow_reentry=True,
    )

    app.add_handler(conversation)

    app.add_handler(
        CommandHandler("admin", admin_command)
    )

    app.add_handler(
        CallbackQueryHandler(callback_router)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    # PHOTO fallback
    app.add_handler(
        MessageHandler(filters.PHOTO, text_router)
    )

    app.add_error_handler(error_handler)

    # Ավելի արագ ստուգում՝ ամեն 10 վայրկյանը մեկ։
    # Իրական փակումը կատարվում է 180 վրկ inactivity-ից հետո։
    if app.job_queue:
        app.job_queue.run_repeating(
            inactivity_cleanup,
            interval=10,
            first=10,
        )
    else:
        log.warning(
            "JobQueue unavailable. Install python-telegram-bot[job-queue]."
        )

    log.info("Together bot started")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

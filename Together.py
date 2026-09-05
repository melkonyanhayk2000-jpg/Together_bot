import os
import sqlite3
import logging
import html
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# TOGETHR — Telegram Dating / Social Bot
# Complete bot-only version
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

CONFIG_DB_FILE = os.getenv("DB_FILE", "").strip()
if CONFIG_DB_FILE:
    DB_FILE = CONFIG_DB_FILE
elif os.path.isdir("/data"):
    DB_FILE = "/data/togethr.db"
else:
    DB_FILE = os.path.join(os.getcwd(), "data", "togethr.db")

BACKUP_DIR = os.getenv(
    "BACKUP_DIR",
    "/data/backups" if DB_FILE.startswith("/data/") else os.path.join(os.path.dirname(DB_FILE), "backups"),
).strip()

def ensure_storage():
    db_dir = os.path.dirname(os.path.abspath(DB_FILE))
    os.makedirs(db_dir, exist_ok=True)
    os.makedirs(os.path.abspath(BACKUP_DIR), exist_ok=True)
    test_file = os.path.join(db_dir, ".write_test")
    try:
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
    except Exception as exc:
        raise RuntimeError(
            f"Database directory is not writable: {db_dir}. "
            f"Check Railway Volume mount path /data. Error: {exc}"
        ) from exc

def db():
    ensure_storage()
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("togethr")


# ============================================================
# TIME
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def now_iso():
    return utc_now().isoformat()


def fmt_time(value):
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
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
            created_at TEXT,
            last_active TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS swipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT,
            UNIQUE(from_user, to_user)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1 INTEGER NOT NULL,
            user2 INTEGER NOT NULL,
            created_at TEXT,
            UNIQUE(user1, user2)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            text TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter INTEGER NOT NULL,
            reported INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cur.execute(
        "INSERT OR IGNORE INTO bot_settings(key, value) VALUES (?, ?)",
        ("admin_activity_notifications", "1"),
    )

    # Migration for old databases.
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_active TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


# ============================================================
# DATABASE HELPERS
# ============================================================

def db_log(user_id, action, details=""):
    conn = db()
    conn.execute(
        """
        INSERT INTO activity_logs(user_id, action, details, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, action, details, now_iso()),
    )
    conn.commit()
    conn.close()


def user_label(user_id):
    user = get_user(user_id)
    if not user:
        return f"ID {user_id}"

    name = user["name"] or "Անանուն"
    username = user["username"]
    if username:
        return f"{name} (@{username}, ID {user_id})"
    return f"{name} (ID {user_id})"


def admin_activity_notifications_enabled():
    conn = db()
    row = conn.execute(
        "SELECT value FROM bot_settings WHERE key=?",
        ("admin_activity_notifications",),
    ).fetchone()
    conn.close()
    return bool(row and row["value"] == "1")


def set_admin_activity_notifications(enabled):
    conn = db()
    conn.execute(
        """
        INSERT INTO bot_settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        ("admin_activity_notifications", "1" if enabled else "0"),
    )
    conn.commit()
    conn.close()


def admin_activity_toggle_keyboard():
    enabled = admin_activity_notifications_enabled()
    status = "🟢 Միացված է" if enabled else "🔴 Անջատված է"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔔 Գործողությունների հաղորդագրություններ՝ {status}",
            callback_data="admin:toggle_activity",
        )],
        [InlineKeyboardButton("⬅️ Admin մենյու", callback_data="admin:menu")],
    ])


async def admin_activity_settings_message(query):
    enabled = admin_activity_notifications_enabled()
    status = "🟢 Միացված" if enabled else "🔴 Անջատված"
    text = (
        "🔔 <b>Բոտի ակտիվության հաղորդագրություններ</b>\n\n"
        f"Կարգավիճակ՝ <b>{status}</b>\n\n"
        "Երբ միացված է, Admin-ը Telegram-ում կստանա "
        "բոտում կատարվող գրանցումների, պրոֆիլների, հավանումների, "
        "Match-երի, հաղորդագրությունների, բողոքների և այլ գործողությունների "
        "անմիջական ծանուցումներ։\n\n"
        "⚠️ Անջատելու դեպքում գործողությունները չեն կորչի․ "
        "դրանք կշարունակեն պահպանվել «📋 Վերջին գործողություններ» բաժնում։"
    )
    await safe_edit_to_text(query, text, admin_activity_toggle_keyboard())


async def admin_notify(text):
    if not ADMIN_ID or not admin_activity_notifications_enabled():
        return

    try:
        # Telegram message limit protection.
        text = str(text)
        if len(text) > 3900:
            text = text[:3900] + "\n…"
        return await application.bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Could not notify admin")


async def log_admin(user_id, action, details=""):
    db_log(user_id, action, details)

    label = html.escape(user_label(user_id))
    safe_action = html.escape(str(action))
    safe_details = html.escape(str(details))

    await admin_notify(
        "🔔 <b>Բոտի ակտիվություն</b>\n\n"
        f"👤 <b>Օգտատեր</b>՝ {label}\n"
        f"⚙️ <b>Գործողություն</b>՝ {safe_action}\n"
        f"📝 <b>Տեղեկություն</b>՝ {safe_details}\n"
        f"🕐 <b>Ժամանակ</b>՝ {html.escape(fmt_time(now_iso()))}"
    )


def create_user(tg_user):
    conn = db()
    existing = conn.execute(
        "SELECT id FROM users WHERE id = ?",
        (tg_user.id,),
    ).fetchone()

    if not existing:
        conn.execute(
            """
            INSERT INTO users(
                id, username, name, created_at, last_active
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                tg_user.id,
                tg_user.username,
                tg_user.first_name or "",
                now_iso(),
                now_iso(),
            ),
        )
        conn.commit()
        created = True
    else:
        conn.execute(
            """
            UPDATE users
            SET username = ?, last_active = ?
            WHERE id = ?
            """,
            (tg_user.username, now_iso(), tg_user.id),
        )
        conn.commit()
        created = False

    conn.close()
    return created


def get_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def update_user(user_id, **fields):
    if not fields:
        return

    allowed = {
        "username",
        "name",
        "age",
        "city",
        "gender",
        "looking_for",
        "about",
        "photo_file_id",
        "banned",
        "last_active",
    }

    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return

    fields["last_active"] = now_iso()

    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]

    conn = db()
    conn.execute(
        f"UPDATE users SET {columns} WHERE id = ?",
        values,
    )
    conn.commit()
    conn.close()


def update_last_active(user_id):
    conn = db()
    conn.execute(
        "UPDATE users SET last_active = ? WHERE id = ?",
        (now_iso(), user_id),
    )
    conn.commit()
    conn.close()


def is_banned(user_id):
    user = get_user(user_id)
    return bool(user and user["banned"])


def profile_is_complete(user):
    if not user:
        return False

    required = [
        user["name"],
        user["age"],
        user["city"],
        user["gender"],
        user["looking_for"],
        user["about"],
        user["photo_file_id"],
    ]
    return all(required)


# ============================================================
# TEXT / KEYBOARDS
# ============================================================

MAIN_MENU = [
    ["👤 Իմ պրոֆիլը", "🔎 Գտնել մարդկանց"],
    ["❤️ Իմ Match-երը", "✏️ Խմբագրել պրոֆիլը"],
]


def main_keyboard(user_id=None):
    menu = [row[:] for row in MAIN_MENU]
    if user_id is not None and admin_only(user_id):
        menu.append(["🛡️ Admin մենյու"])
    return ReplyKeyboardMarkup(
        menu,
        resize_keyboard=True,
        is_persistent=True,
    )


def home_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")]
    ])


def profile_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Խմբագրել", callback_data="edit_profile"),
            InlineKeyboardButton("🔎 Գտնել մարդկանց", callback_data="discover"),
        ],
        [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")],
    ])


def gender_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨 Տղամարդ", callback_data="gender:Տղամարդ"),
            InlineKeyboardButton("👩 Կին", callback_data="gender:Կին"),
        ],
        [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")],
    ])


def looking_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨 Տղամարդ", callback_data="looking:Տղամարդ"),
            InlineKeyboardButton("👩 Կին", callback_data="looking:Կին"),
        ],
        [InlineKeyboardButton("⬅️ Նախորդ", callback_data="profile_back:gender")],
        [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")],
    ])


def profile_back_keyboard(previous_step):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Նախորդ", callback_data=f"profile_back:{previous_step}")],
        [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")],
    ])


def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Գլխավոր վիճակագրություն", callback_data="admin:stats"),
        ],
        [
            InlineKeyboardButton("👥 Օգտատերեր", callback_data="admin:users"),
            InlineKeyboardButton("🟢 Ակտիվներ", callback_data="admin:active"),
        ],
        [
            InlineKeyboardButton("🆕 Նոր օգտատերեր", callback_data="admin:new"),
            InlineKeyboardButton("🚫 Արգելափակվածներ", callback_data="admin:banned"),
        ],
        [
            InlineKeyboardButton("❤️ Հավանումներ", callback_data="admin:likes"),
            InlineKeyboardButton("⭐ Super Like-եր", callback_data="admin:super"),
        ],
        [
            InlineKeyboardButton("💕 Match-եր", callback_data="admin:matches"),
            InlineKeyboardButton("💬 Չատի հաղորդագրություններ", callback_data="admin:messages"),
        ],
        [
            InlineKeyboardButton("🔎 Պրոֆիլների դիտումներ", callback_data="admin:views"),
            InlineKeyboardButton("🚨 Բողոքներ", callback_data="admin:reports"),
        ],
        [
            InlineKeyboardButton("📋 Վերջին գործողություններ", callback_data="admin:activity"),
        ],
        [
            InlineKeyboardButton("🔔 Ակտիվության հաղորդագրություններ", callback_data="admin:activity_settings"),
        ],
        [
            InlineKeyboardButton("🔄 Թարմացնել", callback_data="admin:menu"),
            InlineKeyboardButton("🏠 Բոտի գլխավոր մենյու", callback_data="home"),
        ],
    ])


def admin_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Admin մենյու", callback_data="admin:menu")],
    ])


def swipe_keyboard(target_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Բաց թողնել", callback_data=f"swipe:pass:{target_id}"),
            InlineKeyboardButton("❤️ Հավանել", callback_data=f"swipe:like:{target_id}"),
        ],
        [
            InlineKeyboardButton("⭐ Super Like", callback_data=f"swipe:super:{target_id}"),
        ],
        [
            InlineKeyboardButton("🚨 Բողոքել", callback_data=f"report:{target_id}"),
        ],
        [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")],
    ])


def match_keyboard(match_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Բացել չատը", callback_data=f"chat:{match_id}")],
        [InlineKeyboardButton("🔎 Գտնել մարդկանց", callback_data="discover")],
        [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")],
    ])


# ============================================================
# PROFILE
# ============================================================

def profile_text(user):
    gender = user["gender"] or "—"
    looking = user["looking_for"] or "—"

    return (
        "👤 <b>Իմ պրոֆիլը</b>\n\n"
        f"🧑 <b>Անուն</b>՝ {html.escape(user['name'] or '—')}\n"
        f"🎂 <b>Տարիք</b>՝ {user['age'] or '—'}\n"
        f"📍 <b>Քաղաք</b>՝ {html.escape(user['city'] or '—')}\n"
        f"⚧ <b>Սեռ</b>՝ {html.escape(gender)}\n"
        f"❤️ <b>Փնտրում է</b>՝ {html.escape(looking)}\n"
        f"📝 <b>Իմ մասին</b>՝ {html.escape(user['about'] or '—')}"
    )


async def show_my_profile_message(update, context):
    user = get_user(update.effective_user.id)

    if not user:
        create_user(update.effective_user)
        user = get_user(update.effective_user.id)

    if not profile_is_complete(user):
        await update.message.reply_text(
            "⚠️ Պրոֆիլդ դեռ ամբողջական չէ։\n\n"
            "Սեղմիր «✏️ Խմբագրել պրոֆիլը»՝ լրացնելու համար։",
            reply_markup=main_keyboard(),
        )
        return

    text = profile_text(user)

    if user["photo_file_id"]:
        await update.message.reply_photo(
            photo=user["photo_file_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=profile_keyboard(),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=profile_keyboard(),
        )

    await log_admin(
        update.effective_user.id,
        "Իմ պրոֆիլի դիտում",
        "Օգտատերը դիտեց իր պրոֆիլը",
    )


async def show_my_profile_callback(query, context):
    user = get_user(query.from_user.id)

    if not user:
        create_user(query.from_user)
        user = get_user(query.from_user.id)

    if not profile_is_complete(user):
        await safe_edit_to_text(
            query,
            "⚠️ Պրոֆիլդ դեռ ամբողջական չէ։\n\n"
            "Օգտագործիր «✏️ Խմբագրել պրոֆիլը»։",
            home_inline_keyboard(),
        )
        return

    text = profile_text(user)

    try:
        if user["photo_file_id"]:
            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=profile_keyboard(),
            )
        else:
            await query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=profile_keyboard(),
            )
    except Exception:
        await safe_edit_to_text(
            query,
            text,
            profile_keyboard(),
        )


async def start_profile(update, context, editing=False):
    user_id = update.effective_user.id

    create_user(update.effective_user)

    context.user_data["editing"] = editing
    context.user_data["step"] = "name"

    if editing:
        title = "✏️ <b>Պրոֆիլի խմբագրում</b>"
    else:
        title = "👋 <b>Բարի գալուստ Togethr</b>"

    await update.message.reply_text(
        f"{title}\n\n"
        "Սկսենք պրոֆիլից։\n\n"
        "1️⃣ Գրիր քո անունը։",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )

    await log_admin(
        user_id,
        "Պրոֆիլի լրացում սկսվեց",
        f"editing={editing}",
    )


async def profile_photo_handler(update, context):
    if context.user_data.get("step") != "photo":
        return False

    user_id = update.effective_user.id
    photo = update.message.photo[-1]

    update_user(
        user_id,
        photo_file_id=photo.file_id,
    )

    context.user_data.pop("step", None)
    context.user_data.pop("editing", None)

    user = get_user(user_id)

    await update.message.reply_text(
        "✅ <b>Պրոֆիլդ պատրաստ է։</b>\n\n"
        "Այժմ կարող ես գտնել մարդկանց և ստանալ Match-եր։",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    if user and user["photo_file_id"]:
        await update.message.reply_photo(
            photo=user["photo_file_id"],
            caption=profile_text(user),
            parse_mode="HTML",
            reply_markup=profile_keyboard(),
        )

    await log_admin(
        user_id,
        "Պրոֆիլն ավարտվեց",
        "Օգտատերը լրացրեց բոլոր հիմնական տվյալները",
    )
    return True


# ============================================================
# DISCOVERY
# ============================================================

def compatible(viewer, candidate):
    if not viewer or not candidate:
        return False

    if viewer["gender"] and viewer["looking_for"]:
        if candidate["gender"] != viewer["looking_for"]:
            return False

    if candidate["gender"] and candidate["looking_for"]:
        if viewer["gender"] != candidate["looking_for"]:
            return False

    return True


def get_next_profile(user_id):
    conn = db()

    since = (utc_now() - timedelta(days=7)).isoformat()

    candidates = conn.execute(
        """
        SELECT *
        FROM users
        WHERE id != ?
          AND banned = 0
          AND last_active IS NOT NULL
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
              WHERE from_user = ?
          )
        ORDER BY RANDOM()
        LIMIT 50
        """,
        (user_id, since, user_id),
    ).fetchall()

    conn.close()

    viewer = get_user(user_id)

    for candidate in candidates:
        if compatible(viewer, candidate):
            return candidate

    return None


async def discover(update, context):
    user_id = update.effective_user.id

    if is_banned(user_id):
        text = "🚫 Քո հաշիվը արգելափակված է։"
        if update.callback_query:
            await safe_edit_to_text(
                update.callback_query,
                text,
                home_inline_keyboard(),
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=main_keyboard(),
            )
        return

    user = get_user(user_id)

    if not user or not profile_is_complete(user):
        text = (
            "⚠️ Նախ պետք է լրացնես քո պրոֆիլը։\n\n"
            "Օգտագործիր «✏️ Խմբագրել պրոֆիլը»։"
        )

        if update.callback_query:
            await safe_edit_to_text(
                update.callback_query,
                text,
                home_inline_keyboard(),
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=main_keyboard(),
            )
        return

    candidate = get_next_profile(user_id)

    if not candidate:
        text = (
            "😔 Այս պահին համապատասխան նոր պրոֆիլներ չկան։\n\n"
            "Փորձիր ավելի ուշ։"
        )

        if update.callback_query:
            await safe_edit_to_text(
                update.callback_query,
                text,
                home_inline_keyboard(),
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=main_keyboard(),
            )
        return

    await log_admin(
        user_id,
        "Պրոֆիլ դիտվեց",
        f"Դիտվող պրոֆիլ՝ {user_label(candidate['id'])}",
    )

    caption = (
        "👤 <b>Նոր պրոֆիլ</b>\n\n"
        f"🧑 <b>{html.escape(candidate['name'])}</b>, "
        f"{candidate['age']}\n"
        f"📍 {html.escape(candidate['city'])}\n\n"
        f"📝 {html.escape(candidate['about'])}"
    )

    if update.callback_query:
        query = update.callback_query
        try:
            await query.message.delete()
        except Exception:
            pass

        await query.message.chat.send_photo(
            photo=candidate["photo_file_id"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=swipe_keyboard(candidate["id"]),
        )
    else:
        await update.message.reply_photo(
            photo=candidate["photo_file_id"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=swipe_keyboard(candidate["id"]),
        )


# ============================================================
# SWIPES / MATCHES
# ============================================================

def save_swipe(from_user, to_user, action):
    conn = db()

    conn.execute(
        """
        INSERT INTO swipes(from_user, to_user, action, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(from_user, to_user)
        DO UPDATE SET action=excluded.action, created_at=excluded.created_at
        """,
        (from_user, to_user, action, now_iso()),
    )

    conn.commit()
    conn.close()


def get_swipe(from_user, to_user):
    conn = db()
    row = conn.execute(
        """
        SELECT * FROM swipes
        WHERE from_user = ? AND to_user = ?
        """,
        (from_user, to_user),
    ).fetchone()
    conn.close()
    return row


def create_match(user_a, user_b):
    user1, user2 = sorted([user_a, user_b])

    conn = db()
    existing = conn.execute(
        """
        SELECT * FROM matches
        WHERE user1 = ? AND user2 = ?
        """,
        (user1, user2),
    ).fetchone()

    if existing:
        conn.close()
        return existing, False

    conn.execute(
        """
        INSERT INTO matches(user1, user2, created_at)
        VALUES (?, ?, ?)
        """,
        (user1, user2, now_iso()),
    )
    conn.commit()

    row = conn.execute(
        """
        SELECT * FROM matches
        WHERE user1 = ? AND user2 = ?
        """,
        (user1, user2),
    ).fetchone()

    conn.close()
    return row, True


def get_match(match_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM matches WHERE id = ?",
        (match_id,),
    ).fetchone()
    conn.close()
    return row


def get_other_user(match, user_id):
    if not match:
        return None

    other_id = match["user2"] if match["user1"] == user_id else match["user1"]
    return get_user(other_id)


async def handle_swipe(query, context, action, target_id):
    user_id = query.from_user.id

    try:
        target_id = int(target_id)
    except ValueError:
        await query.answer("Սխալ պրոֆիլ։", show_alert=True)
        return

    if user_id == target_id:
        await query.answer("Չես կարող գնահատել քո պրոֆիլը։", show_alert=True)
        return

    target = get_user(target_id)
    viewer = get_user(user_id)

    if not target or not viewer:
        await query.answer("Պրոֆիլը հասանելի չէ։", show_alert=True)
        return

    if target["banned"] or not profile_is_complete(target):
        await query.answer("Պրոֆիլը հասանելի չէ։", show_alert=True)
        await discover(update_from_query(query), context)
        return

    await query.answer()

    save_swipe(user_id, target_id, action)

    action_name = {
        "pass": "Բաց թողեց",
        "like": "Հավանեց",
        "super": "Super Like արեց",
    }.get(action, action)

    await log_admin(
        user_id,
        action_name,
        f"Թիրախ՝ {user_label(target_id)}",
    )

    if action == "pass":
        await discover(update_from_query(query), context)
        return

    reciprocal = get_swipe(target_id, user_id)

    if reciprocal and reciprocal["action"] in ("like", "super"):
        match, created = create_match(user_id, target_id)

        if created:
            await log_admin(
                user_id,
                "Նոր Match",
                f"{user_label(user_id)} ↔ {user_label(target_id)} | Match ID {match['id']}",
            )

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "💕 <b>Նոր Match!</b>\n\n"
                        f"Դու և <b>{html.escape(target['name'])}</b>-ը հավանել եք միմյանց։"
                    ),
                    parse_mode="HTML",
                    reply_markup=match_keyboard(match["id"]),
                )
            except Exception:
                pass

            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        "💕 <b>Նոր Match!</b>\n\n"
                        f"Դու և <b>{html.escape(viewer['name'])}</b>-ը հավանել եք միմյանց։"
                    ),
                    parse_mode="HTML",
                    reply_markup=match_keyboard(match["id"]),
                )
            except Exception:
                pass

            try:
                await query.message.edit_caption(
                    caption=(
                        "💕 <b>Match!</b>\n\n"
                        f"Դուք և <b>{html.escape(target['name'])}</b>-ը հավանել եք միմյանց։"
                    ),
                    parse_mode="HTML",
                    reply_markup=match_keyboard(match["id"]),
                )
            except Exception:
                try:
                    await query.message.edit_text(
                        "💕 Match! Դուք հավանել եք միմյանց։",
                        reply_markup=match_keyboard(match["id"]),
                    )
                except Exception:
                    pass
        else:
            await query.answer("Այս Match-ը արդեն ստեղծված է։")

        return

    # Notify target about positive rating.
    if action == "like":
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "❤️ <b>Ինչ-որ մեկը հավանել է քո պրոֆիլը։</b>\n\n"
                    "Գնա «🔎 Գտնել մարդկանց» և շարունակիր ծանոթանալ։"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 Գտնել մարդկանց", callback_data="discover")],
                ]),
            )
        except Exception:
            pass

        await log_admin(
            user_id,
            "Հավանման ծանուցում ուղարկվեց",
            f"Ստացող՝ {user_label(target_id)}",
        )

    elif action == "super":
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "⭐ <b>Դու ստացել ես Super Like!</b>\n\n"
                    "Ինչ-որ մեկը հատուկ հավանել է քո պրոֆիլը։"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 Գտնել մարդկանց", callback_data="discover")],
                ]),
            )
        except Exception:
            pass

        await log_admin(
            user_id,
            "Super Like-ի ծանուցում ուղարկվեց",
            f"Ստացող՝ {user_label(target_id)}",
        )

    await discover(update_from_query(query), context)


def update_from_query(query):
    class FakeUpdate:
        def __init__(self, q):
            self.callback_query = q
            self.effective_user = q.from_user
            self.message = None

    return FakeUpdate(query)


# ============================================================
# MATCHES
# ============================================================

def get_user_matches(user_id):
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE user1 = ? OR user2 = ?
        ORDER BY id DESC
        """,
        (user_id, user_id),
    ).fetchall()
    conn.close()
    return rows


async def show_matches_message(update, context):
    user_id = update.effective_user.id
    matches = get_user_matches(user_id)

    if not matches:
        await update.message.reply_text(
            "💕 <b>Իմ Match-երը</b>\n\n"
            "Դեռ Match չունես։\n"
            "Գնա «🔎 Գտնել մարդկանց» և սկսիր ծանոթանալ։",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    buttons = []

    for match in matches:
        other = get_other_user(match, user_id)
        if not other:
            continue

        buttons.append([
            InlineKeyboardButton(
                f"💬 {other['name']}",
                callback_data=f"chat:{match['id']}",
            )
        ])

    buttons.append([
        InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")
    ])

    await update.message.reply_text(
        "💕 <b>Իմ Match-երը</b>\n\n"
        "Ընտրիր մարդու՝ չատը բացելու համար։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ============================================================
# CHAT
# ============================================================

async def open_chat(query, context, match_id):
    try:
        match_id = int(match_id)
    except ValueError:
        await query.answer("Սխալ Match։", show_alert=True)
        return

    match = get_match(match_id)

    if not match or query.from_user.id not in (match["user1"], match["user2"]):
        await query.answer("Այս Match-ը քոնը չէ։", show_alert=True)
        return

    other = get_other_user(match, query.from_user.id)
    if not other:
        await query.answer("Օգտատերը չի գտնվել։", show_alert=True)
        return

    context.user_data["chat_match_id"] = match_id
    context.user_data.pop("step", None)
    context.user_data.pop("editing", None)

    await query.answer()

    await safe_edit_to_text(
        query,
        (
            "💬 <b>Չատ</b>\n\n"
            f"Դու հիմա խոսում ես <b>{html.escape(other['name'])}</b>-ի հետ։\n\n"
            "Գրիր հաղորդագրություն։\n"
            "Դուրս գալու համար սեղմիր «🏠 Գլխավոր»։"
        ),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")],
            [InlineKeyboardButton("💕 Իմ Match-երը", callback_data="matches")],
        ]),
    )

    await log_admin(
        query.from_user.id,
        "Չատ բացվեց",
        f"Match ID {match_id}, զրուցակից՝ {user_label(other['id'])}",
    )


async def chat_message(update, context):
    user_id = update.effective_user.id
    match_id = context.user_data.get("chat_match_id")

    if not match_id:
        return False

    # Menu commands should escape chat mode.
    if update.message.text in {
        "🏠 Գլխավոր",
        "👤 Իմ պրոֆիլը",
        "🔎 Գտնել մարդկանց",
        "❤️ Իմ Match-երը",
        "✏️ Խմբագրել պրոֆիլը",
    }:
        context.user_data.pop("chat_match_id", None)
        return False

    match = get_match(match_id)

    if not match or user_id not in (match["user1"], match["user2"]):
        context.user_data.pop("chat_match_id", None)
        await update.message.reply_text(
            "⚠️ Չատը հասանելի չէ։",
            reply_markup=main_keyboard(),
        )
        return True

    other = get_other_user(match, user_id)
    if not other:
        return True

    text = update.message.text or ""

    if not text.strip():
        return True

    conn = db()
    conn.execute(
        """
        INSERT INTO messages(match_id, sender_id, text, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (match_id, user_id, text, now_iso()),
    )
    conn.commit()
    conn.close()

    update_last_active(user_id)

    await log_admin(
        user_id,
        "Չատի հաղորդագրություն",
        (
            f"Match ID՝ {match_id}\n"
            f"Ստացող՝ {user_label(other['id'])}\n"
            f"Հաղորդագրություն՝ {text}"
        ),
    )

    try:
        await context.bot.send_message(
            chat_id=other["id"],
            text=(
                f"💬 <b>{html.escape(get_user(user_id)['name'] or 'Օգտատեր')}</b>\n\n"
                f"{html.escape(text)}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Պատասխանել", callback_data=f"chat:{match_id}")]
            ]),
        )
    except Exception:
        pass

    await update.message.reply_text("✅ Ուղարկվեց։")
    return True


# ============================================================
# REPORTS
# ============================================================

def create_report(reporter, reported, reason):
    conn = db()
    conn.execute(
        """
        INSERT INTO reports(
            reporter, reported, reason, status, created_at
        )
        VALUES (?, ?, ?, 'open', ?)
        """,
        (reporter, reported, reason, now_iso()),
    )
    conn.commit()
    conn.close()


async def show_report_options(query, target_id):
    await query.answer()

    await safe_edit_to_text(
        query,
        "🚨 <b>Ինչո՞ւ ես բողոքում այս պրոֆիլից:</b>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Անպատշաճ բովանդակություն", callback_data=f"report_reason:inappropriate:{target_id}")],
            [InlineKeyboardButton("👤 Կեղծ պրոֆիլ", callback_data=f"report_reason:fake:{target_id}")],
            [InlineKeyboardButton("😡 Վիրավորանք / չարաշահում", callback_data=f"report_reason:abuse:{target_id}")],
            [InlineKeyboardButton("❓ Այլ", callback_data=f"report_reason:other:{target_id}")],
            [InlineKeyboardButton("⬅️ Հետ", callback_data="discover")],
        ]),
    )


async def handle_report_reason(query, context, reason, target_id):
    reporter = query.from_user.id

    labels = {
        "inappropriate": "Անպատշաճ բովանդակություն",
        "fake": "Կեղծ պրոֆիլ",
        "abuse": "Վիրավորանք / չարաշահում",
        "other": "Այլ",
    }

    target_id = int(target_id)

    create_report(
        reporter,
        target_id,
        labels.get(reason, reason),
    )

    await log_admin(
        reporter,
        "Բողոք",
        (
            f"Թիրախ՝ {user_label(target_id)}\n"
            f"Պատճառ՝ {labels.get(reason, reason)}"
        ),
    )

    await query.answer("Բողոքը ուղարկվեց Admin-ին։", show_alert=True)

    await safe_edit_to_text(
        query,
        "✅ <b>Բողոքը ուղարկվեց։</b>\n\n"
        "Շնորհակալություն՝ համայնքը անվտանգ պահելու համար։",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Շարունակել", callback_data="discover")],
            [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")],
        ]),
    )


# ============================================================
# SAFE MESSAGE EDIT
# ============================================================

async def safe_edit_to_text(query, text, reply_markup=None):
    try:
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return
    except Exception:
        pass

    try:
        await query.message.edit_caption(
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return
    except Exception:
        pass

    try:
        await query.message.delete()
    except Exception:
        pass

    try:
        await query.message.chat.send_message(
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except Exception:
        pass


# ============================================================
# HOME
# ============================================================

async def show_home(update, context):
    # Important: completely clear temporary workflow states.
    for key in (
        "step",
        "editing",
        "chat_match_id",
        "report_target",
    ):
        context.user_data.pop(key, None)

    text = (
        "❤️ <b>Բարի գալուստ Togethr</b>\n\n"
        "Գտիր նոր մարդկանց, հավանիր պրոֆիլներ և ստացիր Match-եր։\n\n"
        "Ընտրիր գործողություն 👇"
    )

    if update.callback_query:
        await safe_edit_to_text(
            update.callback_query,
            text,
            home_inline_keyboard(),
        )
        try:
            await update.callback_query.message.chat.send_message(
                "🏠 Գլխավոր մենյու",
                reply_markup=main_keyboard(update.effective_user.id),
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )


# ============================================================
# ADMIN
# ============================================================

def admin_only(user_id):
    return ADMIN_ID and user_id == ADMIN_ID


def admin_stats():
    conn = db()

    def count(query, params=()):
        row = conn.execute(query, params).fetchone()
        return row[0] if row else 0

    now = utc_now()
    day = (now - timedelta(days=1)).isoformat()
    week = (now - timedelta(days=7)).isoformat()
    month = (now - timedelta(days=30)).isoformat()

    stats = {
        "users": count("SELECT COUNT(*) FROM users"),
        "completed": count("SELECT COUNT(*) FROM users WHERE name IS NOT NULL AND age IS NOT NULL AND city IS NOT NULL AND gender IS NOT NULL AND looking_for IS NOT NULL AND about IS NOT NULL AND photo_file_id IS NOT NULL"),
        "active_24": count("SELECT COUNT(*) FROM users WHERE last_active >= ?", (day,)),
        "active_7": count("SELECT COUNT(*) FROM users WHERE last_active >= ?", (week,)),
        "active_30": count("SELECT COUNT(*) FROM users WHERE last_active >= ?", (month,)),
        "banned": count("SELECT COUNT(*) FROM users WHERE banned = 1"),
        "likes": count("SELECT COUNT(*) FROM swipes WHERE action = 'like'"),
        "super": count("SELECT COUNT(*) FROM swipes WHERE action = 'super'"),
        "passes": count("SELECT COUNT(*) FROM swipes WHERE action = 'pass'"),
        "matches": count("SELECT COUNT(*) FROM matches"),
        "messages": count("SELECT COUNT(*) FROM messages"),
        "reports_open": count("SELECT COUNT(*) FROM reports WHERE status = 'open'"),
        "views": count("SELECT COUNT(*) FROM activity_logs WHERE action = 'Պրոֆիլ դիտվեց'"),
        "new_today": count("SELECT COUNT(*) FROM users WHERE created_at >= ?", (day,)),
        "likes_today": count("SELECT COUNT(*) FROM swipes WHERE action = 'like' AND created_at >= ?", (day,)),
        "matches_today": count("SELECT COUNT(*) FROM matches WHERE created_at >= ?", (day,)),
        "messages_today": count("SELECT COUNT(*) FROM messages WHERE created_at >= ?", (day,)),
        "reports_today": count("SELECT COUNT(*) FROM reports WHERE created_at >= ?", (day,)),
    }

    conn.close()
    return stats


async def admin_menu_message(update, context):
    if not admin_only(update.effective_user.id):
        await update.message.reply_text("⛔ Այս բաժինը հասանելի է միայն Admin-ին։")
        return

    await update.message.reply_text(
        "🛡️ <b>Togethr — Admin կառավարում</b>\n\n"
        "Այստեղ կարող ես տեսնել բոտի ամբողջ ակտիվությունը։\n"
        "Ընտրիր բաժինը 👇",
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard(),
    )


async def admin_stats_text():
    s = admin_stats()

    return (
        "📊 <b>Գլխավոր վիճակագրություն</b>\n\n"
        "👥 <b>Օգտատերեր</b>\n"
        f"• Ընդհանուր՝ {s['users']}\n"
        f"• Ամբողջական պրոֆիլներ՝ {s['completed']}\n"
        f"• Ակտիվ 24 ժամում՝ {s['active_24']}\n"
        f"• Ակտիվ 7 օրում՝ {s['active_7']}\n"
        f"• Ակտիվ 30 օրում՝ {s['active_30']}\n"
        f"• Արգելափակված՝ {s['banned']}\n\n"
        "❤️ <b>Գնահատումներ</b>\n"
        f"• Հավանումներ՝ {s['likes']}\n"
        f"• Super Like-եր՝ {s['super']}\n"
        f"• Բաց թողումներ՝ {s['passes']}\n"
        f"• Պրոֆիլների դիտումներ՝ {s['views']}\n\n"
        "💕 <b>Match / Չատ</b>\n"
        f"• Match-եր՝ {s['matches']}\n"
        f"• Հաղորդագրություններ՝ {s['messages']}\n\n"
        "🚨 <b>Բողոքներ</b>\n"
        f"• Բաց բողոքներ՝ {s['reports_open']}\n\n"
        "📅 <b>Վերջին 24 ժամ</b>\n"
        f"• Նոր օգտատերեր՝ {s['new_today']}\n"
        f"• Հավանումներ՝ {s['likes_today']}\n"
        f"• Match-եր՝ {s['matches_today']}\n"
        f"• Հաղորդագրություններ՝ {s['messages_today']}\n"
        f"• Բողոքներ՝ {s['reports_today']}\n"
    )


async def admin_users_text():
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM users
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    conn.close()

    if not rows:
        return "👥 <b>Օգտատերեր</b>\n\nԴեռ օգտատերեր չկան։"

    lines = ["👥 <b>Վերջին օգտատերերը</b>\n"]

    for i, user in enumerate(rows, 1):
        status = "🚫" if user["banned"] else "🟢"
        name = html.escape(user["name"] or "Անանուն")
        username = f"@{html.escape(user['username'])}" if user["username"] else "—"

        lines.append(
            f"{i}. {status} <b>{name}</b>\n"
            f"   ID՝ <code>{user['id']}</code> | {username}\n"
            f"   📍 {html.escape(user['city'] or '—')} | "
            f"🎂 {user['age'] or '—'}\n"
            f"   ⚧ {html.escape(user['gender'] or '—')} → "
            f"{html.escape(user['looking_for'] or '—')}\n"
            f"   🕐 {fmt_time(user['last_active'])}"
        )

    return "\n".join(lines)


async def admin_active_text():
    conn = db()
    since = (utc_now() - timedelta(hours=24)).isoformat()

    rows = conn.execute(
        """
        SELECT *
        FROM users
        WHERE banned = 0 AND last_active >= ?
        ORDER BY last_active DESC
        LIMIT 30
        """,
        (since,),
    ).fetchall()

    conn.close()

    lines = ["🟢 <b>Վերջին 24 ժամվա ակտիվ օգտատերեր</b>\n"]

    if not rows:
        lines.append("Ակտիվ օգտատերեր չկան։")
    else:
        for user in rows:
            lines.append(
                f"• {html.escape(user['name'] or 'Անանուն')} "
                f"(ID {user['id']}) — {fmt_time(user['last_active'])}"
            )

    return "\n".join(lines)


async def admin_new_text():
    conn = db()
    since = (utc_now() - timedelta(days=7)).isoformat()

    rows = conn.execute(
        """
        SELECT *
        FROM users
        WHERE created_at >= ?
        ORDER BY created_at DESC
        LIMIT 30
        """,
        (since,),
    ).fetchall()

    conn.close()

    lines = ["🆕 <b>Վերջին 7 օրվա նոր օգտատերեր</b>\n"]

    if not rows:
        lines.append("Նոր օգտատերեր չկան։")
    else:
        for user in rows:
            lines.append(
                f"• {html.escape(user['name'] or 'Անանուն')} "
                f"(ID {user['id']}) — {fmt_time(user['created_at'])}"
            )

    return "\n".join(lines)


async def admin_banned_text():
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM users
        WHERE banned = 1
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    conn.close()

    lines = ["🚫 <b>Արգելափակված օգտատերեր</b>\n"]

    if not rows:
        lines.append("Արգելափակվածներ չկան։")
    else:
        for user in rows:
            lines.append(
                f"• {html.escape(user['name'] or 'Անանուն')} "
                f"(ID {user['id']})"
            )

    return "\n".join(lines)


async def admin_swipes_text(action, title):
    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM swipes
        WHERE action = ?
        ORDER BY id DESC
        LIMIT 30
        """,
        (action,),
    ).fetchall()

    conn.close()

    lines = [f"{title}\n"]

    if not rows:
        lines.append("Տվյալներ չկան։")
    else:
        for row in rows:
            lines.append(
                f"• {html.escape(user_label(row['from_user']))}\n"
                f"  → {html.escape(user_label(row['to_user']))}\n"
                f"  🕐 {fmt_time(row['created_at'])}"
            )

    return "\n".join(lines)


async def admin_matches_text():
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM matches
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    conn.close()

    lines = ["💕 <b>Վերջին Match-երը</b>\n"]

    if not rows:
        lines.append("Match-եր չկան։")
    else:
        for row in rows:
            lines.append(
                f"• #{row['id']} — "
                f"{html.escape(user_label(row['user1']))} ↔ "
                f"{html.escape(user_label(row['user2']))}\n"
                f"  🕐 {fmt_time(row['created_at'])}"
            )

    return "\n".join(lines)


async def admin_messages_text():
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM messages
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    conn.close()

    lines = ["💬 <b>Վերջին չատի հաղորդագրությունները</b>\n"]

    if not rows:
        lines.append("Հաղորդագրություններ չկան։")
    else:
        for row in rows:
            text = row["text"] or ""
            if len(text) > 180:
                text = text[:180] + "…"

            lines.append(
                f"• Match #{row['match_id']} | "
                f"{html.escape(user_label(row['sender_id']))}\n"
                f"  «{html.escape(text)}»\n"
                f"  🕐 {fmt_time(row['created_at'])}"
            )

    return "\n".join(lines)


async def admin_views_text():
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM activity_logs
        WHERE action = 'Պրոֆիլ դիտվեց'
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    conn.close()

    lines = ["🔎 <b>Վերջին պրոֆիլների դիտումներ</b>\n"]

    if not rows:
        lines.append("Դիտումներ չկան։")
    else:
        for row in rows:
            lines.append(
                f"• {html.escape(user_label(row['user_id']))}\n"
                f"  {html.escape(row['details'] or '')}\n"
                f"  🕐 {fmt_time(row['created_at'])}"
            )

    return "\n".join(lines)


async def admin_reports_text():
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM reports
        WHERE status = 'open'
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    conn.close()

    lines = ["🚨 <b>Բաց բողոքներ</b>\n"]

    if not rows:
        lines.append("Բաց բողոքներ չկան։")
    else:
        for row in rows:
            lines.append(
                f"• Բողոք #{row['id']}\n"
                f"  👤 Բողոքող՝ {html.escape(user_label(row['reporter']))}\n"
                f"  🚨 Թիրախ՝ {html.escape(user_label(row['reported']))}\n"
                f"  📝 {html.escape(row['reason'])}\n"
                f"  🕐 {fmt_time(row['created_at'])}"
            )

    return "\n".join(lines)


async def admin_activity_text():
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM activity_logs
        ORDER BY id DESC
        LIMIT 40
        """
    ).fetchall()
    conn.close()

    lines = ["📋 <b>Վերջին գործողություններ</b>\n"]

    if not rows:
        lines.append("Գործողություններ չկան։")
    else:
        for row in rows:
            details = row["details"] or ""
            if len(details) > 250:
                details = details[:250] + "…"

            lines.append(
                f"#{row['id']} | 🕐 {fmt_time(row['created_at'])}\n"
                f"👤 {html.escape(user_label(row['user_id']))}\n"
                f"⚙️ {html.escape(row['action'])}\n"
                f"📝 {html.escape(details)}\n"
            )

    return "\n".join(lines)


async def admin_callback(query, context):
    if not admin_only(query.from_user.id):
        await query.answer("⛔ Մուտքը թույլատրված չէ։", show_alert=True)
        return

    action = query.data.split(":", 1)[1] if ":" in query.data else "menu"

    if action == "activity_settings":
        await query.answer()
        await admin_activity_settings_message(query)
        return

    if action == "toggle_activity":
        new_value = not admin_activity_notifications_enabled()
        set_admin_activity_notifications(new_value)
        await query.answer(
            "🔔 Միացված է։" if new_value else "🔕 Անջատված է։",
            show_alert=True,
        )
        await admin_activity_settings_message(query)
        return

    if action == "menu":
        await query.answer()
        await safe_edit_to_text(
            query,
            "🛡️ <b>Togethr — Admin կառավարում</b>\n\n"
            "Ընտրիր անհրաժեշտ բաժինը 👇",
            admin_menu_keyboard(),
        )
        return

    await query.answer()

    if action == "stats":
        text = await admin_stats_text()
    elif action == "users":
        text = await admin_users_text()
    elif action == "active":
        text = await admin_active_text()
    elif action == "new":
        text = await admin_new_text()
    elif action == "banned":
        text = await admin_banned_text()
    elif action == "likes":
        text = await admin_swipes_text("like", "❤️ <b>Հավանումներ</b>")
    elif action == "super":
        text = await admin_swipes_text("super", "⭐ <b>Super Like-եր</b>")
    elif action == "matches":
        text = await admin_matches_text()
    elif action == "messages":
        text = await admin_messages_text()
    elif action == "views":
        text = await admin_views_text()
    elif action == "reports":
        text = await admin_reports_text()
    elif action == "activity":
        text = await admin_activity_text()
    else:
        text = "⚠️ Անհայտ Admin բաժին։"

    # Telegram limit protection.
    if len(text) > 3900:
        text = text[:3900] + "\n…"

    await safe_edit_to_text(
        query,
        text,
        admin_back_keyboard(),
    )


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def admin_command(update, context):
    await admin_menu_message(update, context)


async def users_command(update, context):
    if not admin_only(update.effective_user.id):
        return

    text = await admin_users_text()
    await update.message.reply_text(
        text[:3900],
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )


async def reports_command(update, context):
    if not admin_only(update.effective_user.id):
        return

    text = await admin_reports_text()
    await update.message.reply_text(
        text[:3900],
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )


async def ban_command(update, context):
    if not admin_only(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Օգտագործում՝ /ban USER_ID"
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Սխալ USER_ID։")
        return

    target = get_user(target_id)

    if not target:
        await update.message.reply_text("Օգտատերը չի գտնվել։")
        return

    update_user(target_id, banned=1)

    await update.message.reply_text(
        f"🚫 Օգտատեր {target_id}-ը արգելափակվեց։"
    )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="🚫 Քո հաշիվը արգելափակվել է Admin-ի կողմից։",
        )
    except Exception:
        pass

    await log_admin(
        update.effective_user.id,
        "Օգտատեր արգելափակվեց",
        f"Թիրախ՝ {user_label(target_id)}",
    )


async def unban_command(update, context):
    if not admin_only(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Օգտագործում՝ /unban USER_ID"
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Սխալ USER_ID։")
        return

    target = get_user(target_id)

    if not target:
        await update.message.reply_text("Օգտատերը չի գտնվել։")
        return

    update_user(target_id, banned=0)

    await update.message.reply_text(
        f"✅ Օգտատեր {target_id}-ը ապաբլոկավորվեց։"
    )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="✅ Քո հաշիվը կրկին ակտիվացված է։",
        )
    except Exception:
        pass

    await log_admin(
        update.effective_user.id,
        "Օգտատեր ապաբլոկավորվեց",
        f"Թիրախ՝ {user_label(target_id)}",
    )


async def activity_command(update, context):
    if not admin_only(update.effective_user.id):
        return

    text = await admin_activity_text()

    await update.message.reply_text(
        text[:3900],
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )


# ============================================================
# USER TEXT ROUTER
# ============================================================

async def text_router(update, context):
    user_id = update.effective_user.id

    if is_banned(user_id):
        await update.message.reply_text(
            "🚫 Քո հաշիվը արգելափակված է։"
        )
        return

    create_user(update.effective_user)
    update_last_active(user_id)

    text = update.message.text or ""

    # Escape chat mode first only if this isn't a main menu action.
    if context.user_data.get("chat_match_id"):
        if text not in {
            "🏠 Գլխավոր",
            "👤 Իմ պրոֆիլը",
            "🔎 Գտնել մարդկանց",
            "❤️ Իմ Match-երը",
            "✏️ Խմբագրել պրոֆիլը",
        }:
            handled = await chat_message(update, context)
            if handled:
                return
        else:
            context.user_data.pop("chat_match_id", None)

    # Main menu.
    if text == "🏠 Գլխավոր":
        await show_home(update, context)
        return

    if text == "👤 Իմ պրոֆիլը":
        await show_my_profile_message(update, context)
        return

    if text == "🔎 Գտնել մարդկանց":
        await discover(update, context)
        return

    if text == "❤️ Իմ Match-երը":
        await show_matches_message(update, context)
        return

    if text == "✏️ Խմբագրել պրոֆիլը":
        await start_profile(update, context, editing=True)
        return

    if text == "🛡️ Admin մենյու":
        await admin_menu_message(update, context)
        return

    # Profile flow.
    step = context.user_data.get("step")

    if step == "name":
        if len(text.strip()) < 2:
            await update.message.reply_text(
                "⚠️ Անունը պետք է առնվազն 2 նիշ լինի։"
            )
            return

        update_user(user_id, name=text.strip())
        context.user_data["step"] = "age"

        await update.message.reply_text(
            "🎂 Քանի՞ տարեկան ես։\n\n"
            "Գրիր թիվ՝ 18-ից 99։",
            reply_markup=profile_back_keyboard("name"),
        )

        await log_admin(
            user_id,
            "Պրոֆիլի անունը փոխվեց",
            f"Նոր անուն՝ {text.strip()}",
        )
        return

    if step == "age":
        try:
            age = int(text.strip())
        except ValueError:
            await update.message.reply_text(
                "⚠️ Գրիր ճիշտ տարիքը՝ 18-99։"
            )
            return

        if not 18 <= age <= 99:
            await update.message.reply_text(
                "⚠️ Togethr-ը հասանելի է միայն 18+ օգտատերերի համար։"
            )
            return

        update_user(user_id, age=age)
        context.user_data["step"] = "city"

        await update.message.reply_text(
            "📍 Ո՞ր քաղաքում ես ապրում։",
            reply_markup=profile_back_keyboard("age"),
        )

        await log_admin(
            user_id,
            "Պրոֆիլի տարիքը փոխվեց",
            f"Տարիք՝ {age}",
        )
        return

    if step == "city":
        if len(text.strip()) < 2:
            await update.message.reply_text(
                "⚠️ Գրիր քաղաքի անունը։"
            )
            return

        update_user(user_id, city=text.strip())
        context.user_data["step"] = "gender"

        await update.message.reply_text(
            "⚧ <b>Ո՞րն է քո սեռը։</b>",
            parse_mode="HTML",
            reply_markup=gender_keyboard(),
        )

        await log_admin(
            user_id,
            "Պրոֆիլի քաղաքը փոխվեց",
            f"Քաղաք՝ {text.strip()}",
        )
        return

    if step == "about":
        if len(text.strip()) < 2:
            await update.message.reply_text(
                "⚠️ Մի փոքր պատմիր քո մասին։"
            )
            return

        update_user(user_id, about=text.strip())
        context.user_data["step"] = "photo"

        await update.message.reply_text(
            "📸 Այժմ ուղարկիր քո լուսանկարը։",
            reply_markup=profile_back_keyboard("about"),
        )

        await log_admin(
            user_id,
            "Պրոֆիլի նկարագրությունը փոխվեց",
            f"Նկարագրություն՝ {text.strip()}",
        )
        return

    await update.message.reply_text(
        "Ընտրիր գործողություն 👇",
        reply_markup=main_keyboard(),
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):
    query = update.callback_query
    data = query.data or ""
    user_id = query.from_user.id

    if is_banned(user_id):
        await query.answer("🚫 Քո հաշիվը արգելափակված է։", show_alert=True)
        return

    update_last_active(user_id)

    # Admin menu.
    if data.startswith("admin:"):
        await admin_callback(query, context)
        return

    # Home.
    if data == "home":
        await query.answer()
        await show_home(update, context)
        return

    # My profile.
    if data == "my_profile":
        await query.answer()
        await show_my_profile_callback(query, context)
        return

    # Discover.
    if data == "discover":
        await query.answer()
        await discover(update, context)
        return

    # Matches.
    if data == "matches":
        await query.answer()
        matches = get_user_matches(user_id)

        if not matches:
            await safe_edit_to_text(
                query,
                "💕 <b>Իմ Match-երը</b>\n\n"
                "Դեռ Match չունես։",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 Գտնել մարդկանց", callback_data="discover")],
                    [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")],
                ]),
            )
            return

        buttons = []
        for match in matches:
            other = get_other_user(match, user_id)
            if other:
                buttons.append([
                    InlineKeyboardButton(
                        f"💬 {other['name']}",
                        callback_data=f"chat:{match['id']}",
                    )
                ])

        buttons.append([
            InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")
        ])

        await safe_edit_to_text(
            query,
            "💕 <b>Իմ Match-երը</b>\n\nԸնտրիր զրուցակցին։",
            InlineKeyboardMarkup(buttons),
        )
        return

    # Start / edit profile.
    if data == "edit_profile":
        await query.answer()
        context.user_data["editing"] = True
        context.user_data["step"] = "name"

        await safe_edit_to_text(
            query,
            "✏️ <b>Պրոֆիլի խմբագրում</b>\n\n"
            "Գրիր քո անունը։",
            profile_back_keyboard("name"),
        )
        return

    # Gender.
    if data.startswith("gender:"):
        gender = data.split(":", 1)[1]

        update_user(user_id, gender=gender)
        context.user_data["step"] = "looking"

        await query.answer("Պահպանվեց։")

        await safe_edit_to_text(
            query,
            "❤️ <b>Ո՞ւմ ես փնտրում։</b>",
            looking_keyboard(),
        )

        await log_admin(
            user_id,
            "Պրոֆիլի սեռը ընտրվեց",
            f"Սեռ՝ {gender}",
        )
        return

    # Looking for.
    if data.startswith("looking:"):
        looking = data.split(":", 1)[1]

        update_user(user_id, looking_for=looking)
        context.user_data["step"] = "about"

        await query.answer("Պահպանվեց։")

        await safe_edit_to_text(
            query,
            "📝 <b>Պատմիր մի փոքր քո մասին։</b>\n\n"
            "Գրիր առնվազն 2 նիշ։",
            profile_back_keyboard("looking"),
        )

        await log_admin(
            user_id,
            "Փնտրած սեռը ընտրվեց",
            f"Փնտրում է՝ {looking}",
        )
        return

    # Profile back navigation.
    if data.startswith("profile_back:"):
        target_step = data.split(":", 1)[1]

        valid_steps = {
            "name",
            "age",
            "city",
            "gender",
            "looking",
            "about",
        }

        if target_step not in valid_steps:
            target_step = "name"

        context.user_data["step"] = target_step

        await query.answer()

        prompts = {
            "name": "🧑 <b>Գրիր անունը։</b>",
            "age": "🎂 <b>Գրիր տարիքը՝ 18-99։</b>",
            "city": "📍 <b>Գրիր քաղաքը։</b>",
            "gender": "⚧ <b>Ընտրիր սեռը։</b>",
            "looking": "❤️ <b>Ու՞մ ես փնտրում։</b>",
            "about": "📝 <b>Գրիր քո մասին։</b>",
        }

        if target_step == "gender":
            await safe_edit_to_text(
                query,
                prompts[target_step],
                gender_keyboard(),
            )
        elif target_step == "looking":
            await safe_edit_to_text(
                query,
                prompts[target_step],
                looking_keyboard(),
            )
        else:
            previous = {
                "name": "name",
                "age": "name",
                "city": "age",
                "about": "looking",
            }.get(target_step, "name")

            await safe_edit_to_text(
                query,
                prompts[target_step],
                profile_back_keyboard(previous),
            )
        return

    # Swipe.
    if data.startswith("swipe:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Սխալ գործողություն։", show_alert=True)
            return

        action = parts[1]
        target_id = parts[2]

        await handle_swipe(query, context, action, target_id)
        return

    # Report.
    if data.startswith("report:"):
        target_id = data.split(":", 1)[1]
        await show_report_options(query, target_id)
        return

    # Report reason.
    if data.startswith("report_reason:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Սխալ բողոք։", show_alert=True)
            return

        await handle_report_reason(
            query,
            context,
            parts[1],
            parts[2],
        )
        return

    # Chat.
    if data.startswith("chat:"):
        match_id = data.split(":", 1)[1]
        await open_chat(query, context, match_id)
        return


# ============================================================
# COMMANDS
# ============================================================

async def start_command(update, context):
    user_id = update.effective_user.id

    if is_banned(user_id):
        await update.message.reply_text(
            "🚫 Քո հաշիվը արգելափակված է։"
        )
        return

    created = create_user(update.effective_user)
    update_last_active(user_id)

    user = get_user(user_id)

    await log_admin(
        user_id,
        "Start",
        "Նոր օգտատեր" if created else "Վերադարձավ բոտ",
    )

    if not profile_is_complete(user):
        await update.message.reply_text(
            "❤️ <b>Բարի գալուստ Togethr</b>\n\n"
            "Togethr-ը օգնում է գտնել նոր մարդկանց և ծանոթանալ։\n\n"
            "Սկսելու համար լրացրու քո պրոֆիլը։",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
        await start_profile(update, context, editing=False)
    else:
        await show_home(update, context)


async def help_command(update, context):
    await update.message.reply_text(
        "ℹ️ <b>Togethr-ի օգնություն</b>\n\n"
        "👤 Իմ պրոֆիլը — դիտել պրոֆիլը\n"
        "🔎 Գտնել մարդկանց — տեսնել համապատասխան ակտիվ պրոֆիլներ\n"
        "❤️ Իմ Match-երը — տեսնել փոխադարձ հավանումները\n"
        "✏️ Խմբագրել պրոֆիլը — փոխել տվյալները\n\n"
        "❤️ Հավանել — հավանում ես պրոֆիլը\n"
        "⭐ Super Like — հատուկ հավանում\n"
        "💕 Match — երբ երկու օգտատերերն էլ հավանում են միմյանց\n"
        "💬 Match-ից հետո կարող եք գրել իրար։",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def cancel_command(update, context):
    for key in (
        "step",
        "editing",
        "chat_match_id",
        "report_target",
    ):
        context.user_data.pop(key, None)

    await update.message.reply_text(
        "❌ Գործողությունը չեղարկվեց։",
        reply_markup=main_keyboard(),
    )

    await log_admin(
        update.effective_user.id,
        "Գործողությունը չեղարկվեց",
        "/cancel",
    )


# ============================================================
# PHOTO / NON-TEXT
# ============================================================

async def photo_router(update, context):
    if is_banned(update.effective_user.id):
        return

    if await profile_photo_handler(update, context):
        return

    # If photo is sent in chat, notify user that text chat currently
    # supports text messages only.
    if context.user_data.get("chat_match_id"):
        await update.message.reply_text(
            "ℹ️ Այս տարբերակում չատը աջակցում է միայն տեքստային հաղորդագրություններին։"
        )
        return

    await update.message.reply_text(
        "📸 Լուսանկարը կարող ես ուղարկել միայն պրոֆիլի լուսանկարի դաշտում։"
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    logger.exception(
        "Unhandled exception",
        exc_info=context.error,
    )

    if ADMIN_ID:
        try:
            error_text = str(context.error)
            if len(error_text) > 2500:
                error_text = error_text[:2500] + "…"

            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "🚨 <b>Բոտի սխալ</b>\n\n"
                    f"<code>{html.escape(error_text)}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Could not send error to admin")


# ============================================================
# APPLICATION
# ============================================================

application = None


def main():
    global application

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    init_database()

    application = Application.builder().token(BOT_TOKEN).build()

    # Commands.
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("reports", reports_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("activity", activity_command))

    # Callback buttons.
    application.add_handler(
        CallbackQueryHandler(callback_router)
    )

    # Photos first.
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_router,
        )
    )

    # Text.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Togethr bot started.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()

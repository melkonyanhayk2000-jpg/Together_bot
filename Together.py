import os
import sqlite3
import logging
import html
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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

# ============================================================
# TOGETHR — Telegram Dating / Social Bot
# Complete bot-only version
# Armenian UI + TikTok → Together tracking
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_FILE = os.getenv("DB_FILE", "/data/together.db")

# 3 minutes inactivity -> close active chat, Match remains
UI_CLEANUP_SECONDS = 180

# A profile is considered active for discovery for 7 days
ACTIVE_DAYS = 7

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("togethr")


# ============================================================
# TEMPORARY BOT UI CLEANUP
# ============================================================

def _ui_store(application):
    return getattr(application, "togethr_ui_messages", {})


def track_ui_message(context, message):
    """Register a temporary bot UI message for 3-minute cleanup."""
    if not message or not context or not getattr(context, "application", None):
        return message
    store = _ui_store(context.application)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return message
    store.setdefault(int(chat_id), {})[int(message_id)] = utc_now().timestamp()
    return message


def track_callback_ui(context, update):
    query = getattr(update, "callback_query", None)
    message = getattr(query, "message", None)
    if message is not None:
        track_ui_message(context, message)


async def send_ui_message(update, context, *args, **kwargs):
    message = await update.effective_message.reply_text(*args, **kwargs)
    return track_ui_message(context, message)


async def send_ui_chat_message(update, context, *args, **kwargs):
    message = await update.effective_chat.send_message(*args, **kwargs)
    return track_ui_message(context, message)


async def send_ui_photo(update, context, *args, **kwargs):
    message = await update.effective_chat.send_photo(*args, **kwargs)
    return track_ui_message(context, message)


async def cleanup_ui_messages(context):
    """Delete temporary bot UI messages older than three minutes.

    Match conversation messages are deliberately never registered here.
    Therefore this cleanup cannot delete Match conversation history.
    """
    app = context.application
    store = _ui_store(app)
    if not store:
        return

    now_ts = utc_now().timestamp()
    for chat_id, messages in list(store.items()):
        for message_id, created_ts in list(messages.items()):
            if now_ts - created_ts < UI_CLEANUP_SECONDS:
                continue
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except Exception as exc:
                logger.debug(
                    "UI cleanup could not delete %s/%s: %s",
                    chat_id, message_id, exc,
                )
            finally:
                messages.pop(message_id, None)
        if not messages:
            store.pop(chat_id, None)


# ============================================================
# TIME / DATABASE
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
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)

    with db() as conn:
        conn.execute("""
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
                last_active TEXT,
                acquisition_source TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS swipes (
                from_user INTEGER NOT NULL,
                to_user INTEGER NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(from_user, to_user)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user1 INTEGER NOT NULL,
                user2 INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user1, user2)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter INTEGER NOT NULL,
                reported INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                blocker INTEGER NOT NULL,
                blocked INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(blocker, blocked)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        conn.execute("""
            INSERT OR IGNORE INTO bot_settings(key, value)
            VALUES ('activity_notifications', '1')
        """)

        # Migration for databases created by older Together.py versions.
        try:
            conn.execute(
                "ALTER TABLE users ADD COLUMN last_active TEXT"
            )
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute(
                "ALTER TABLE users ADD COLUMN acquisition_source TEXT"
            )
        except sqlite3.OperationalError:
            pass

        # Helpful indexes
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_last_active "
            "ON users(last_active)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_match "
            "ON messages(match_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_created "
            "ON activity_logs(created_at)"
        )


# ============================================================
# SETTINGS / LOGGING
# ============================================================

def get_setting(key, default=None):
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key=?",
            (key,),
        ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute("""
            INSERT INTO bot_settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))


def activity_notifications_enabled():
    return get_setting("activity_notifications", "1") == "1"


async def notify_admin(context, text):
    if not ADMIN_ID:
        return

    if not activity_notifications_enabled():
        return

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Admin notification failed: %s", exc)


async def log_activity(
    user_id,
    action,
    details="",
    context=None,
    notify=True,
):
    try:
        with db() as conn:
            conn.execute("""
                INSERT INTO activity_logs(
                    user_id, action, details, created_at
                )
                VALUES (?, ?, ?, ?)
            """, (user_id, action, details, now_iso()))
    except Exception:
        logger.exception("Could not write activity log")

    if notify and context and ADMIN_ID and activity_notifications_enabled():
        username = ""
        try:
            user = await context.bot.get_chat(user_id)
            username = f"@{user.username}" if user.username else ""
        except Exception:
            pass

        safe_details = html.escape(details or "")
        await notify_admin(
            context,
            "🔔 <b>Բոտի ակտիվություն</b>\n\n"
            f"👤 ID՝ <code>{user_id}</code>\n"
            f"📛 Username՝ {html.escape(username or '—')}\n"
            f"⚙️ Գործողություն՝ <b>{html.escape(action)}</b>\n"
            f"📝 {safe_details or '—'}",
        )


# ============================================================
# USER / PROFILE HELPERS
# ============================================================

def ensure_user(tg_user):
    timestamp = now_iso()

    with db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE id=?",
            (tg_user.id,),
        ).fetchone()

        if row:
            conn.execute("""
                UPDATE users
                SET username=?, last_active=?
                WHERE id=?
            """, (
                tg_user.username,
                timestamp,
                tg_user.id,
            ))
        else:
            conn.execute("""
                INSERT INTO users(
                    id, username, name, created_at, last_active
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                tg_user.id,
                tg_user.username,
                tg_user.first_name or "",
                timestamp,
                timestamp,
            ))


def touch(user_id):
    with db() as conn:
        conn.execute(
            "UPDATE users SET last_active=? WHERE id=?",
            (now_iso(), user_id),
        )


def get_user(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id=?",
            (user_id,),
        ).fetchone()


def update_user(user_id, **fields):
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
    }

    fields = {
        key: value
        for key, value in fields.items()
        if key in allowed
    }

    if not fields:
        return

    columns = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [user_id]

    with db() as conn:
        conn.execute(
            f"UPDATE users SET {columns} WHERE id=?",
            values,
        )


def profile_complete(user):
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
    return all(value not in (None, "", 0) for value in required)


def set_acquisition_source(user_id, source):
    """
    Store the first valid acquisition source only.
    This prevents later /start links from overwriting the
    original attribution.
    """
    if not source:
        return False

    with db() as conn:
        row = conn.execute(
            "SELECT acquisition_source FROM users WHERE id=?",
            (user_id,),
        ).fetchone()

        if not row:
            return False

        if row["acquisition_source"]:
            return False

        conn.execute("""
            UPDATE users
            SET acquisition_source=?
            WHERE id=?
        """, (source, user_id))

    return True


def is_banned(user_id):
    user = get_user(user_id)
    return bool(user and user["banned"])


# ============================================================
# MATCH / BLOCK / SWIPE HELPERS
# ============================================================

def normalize_pair(user1, user2):
    return tuple(sorted((int(user1), int(user2))))


def get_match(match_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE id=?",
            (match_id,),
        ).fetchone()


def get_match_for_users(user1, user2):
    a, b = normalize_pair(user1, user2)

    with db() as conn:
        return conn.execute("""
            SELECT * FROM matches
            WHERE user1=? AND user2=?
        """, (a, b)).fetchone()


def user_in_match(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT * FROM matches
            WHERE user1=? OR user2=?
            ORDER BY id DESC
            LIMIT 1
        """, (user_id, user_id)).fetchone()


def get_user_matches(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT
                m.*,
                CASE
                    WHEN m.user1=? THEN m.user2
                    ELSE m.user1
                END AS other_user_id
            FROM matches m
            WHERE m.user1=? OR m.user2=?
            ORDER BY m.id DESC
        """, (user_id, user_id, user_id)).fetchall()


def is_blocked(user1, user2):
    with db() as conn:
        row = conn.execute("""
            SELECT 1 FROM blocks
            WHERE blocker=? AND blocked=?
        """, (user1, user2)).fetchone()
    return bool(row)


def are_blocked_either_way(user1, user2):
    with db() as conn:
        row = conn.execute("""
            SELECT 1 FROM blocks
            WHERE (blocker=? AND blocked=?)
               OR (blocker=? AND blocked=?)
        """, (user1, user2, user2, user1)).fetchone()
    return bool(row)


def add_block(blocker, blocked):
    if blocker == blocked:
        return

    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO blocks(
                blocker, blocked, created_at
            )
            VALUES (?, ?, ?)
        """, (blocker, blocked, now_iso()))


def remove_block(blocker, blocked):
    with db() as conn:
        conn.execute("""
            DELETE FROM blocks
            WHERE blocker=? AND blocked=?
        """, (blocker, blocked))


def get_blocked_users(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT u.*
            FROM blocks b
            JOIN users u ON u.id=b.blocked
            WHERE b.blocker=?
            ORDER BY b.created_at DESC
        """, (user_id,)).fetchall()


def compatible(viewer, candidate):
    if not viewer or not candidate:
        return False

    if viewer["gender"] and candidate["looking_for"]:
        if viewer["gender"] != candidate["looking_for"]:
            return False

    if candidate["gender"] and viewer["looking_for"]:
        if candidate["gender"] != viewer["looking_for"]:
            return False

    return True


def get_next_profile(user_id):
    viewer = get_user(user_id)
    if not viewer:
        return None

    cutoff = (
        utc_now() - timedelta(days=ACTIVE_DAYS)
    ).isoformat()

    with db() as conn:
        candidates = conn.execute("""
            SELECT u.*
            FROM users u
            WHERE u.id != ?
              AND u.banned=0
              AND u.last_active >= ?
              AND u.name IS NOT NULL
              AND u.age IS NOT NULL
              AND u.city IS NOT NULL
              AND u.gender IS NOT NULL
              AND u.looking_for IS NOT NULL
              AND u.about IS NOT NULL
              AND u.photo_file_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM swipes s
                  WHERE s.from_user=? AND s.to_user=u.id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM blocks b
                  WHERE (b.blocker=? AND b.blocked=u.id)
                     OR (b.blocker=u.id AND b.blocked=?)
              )
            ORDER BY RANDOM()
            LIMIT 50
        """, (
            user_id,
            cutoff,
            user_id,
            user_id,
            user_id,
        )).fetchall()

    for candidate in candidates:
        if compatible(viewer, candidate):
            return candidate

    return None


def save_swipe(from_user, to_user, action):
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
        """, (
            from_user,
            to_user,
            action,
            now_iso(),
        ))


def get_swipe(from_user, to_user):
    with db() as conn:
        return conn.execute("""
            SELECT * FROM swipes
            WHERE from_user=? AND to_user=?
        """, (from_user, to_user)).fetchone()


def create_match_if_needed(user1, user2):
    a, b = normalize_pair(user1, user2)

    with db() as conn:
        existing = conn.execute("""
            SELECT * FROM matches
            WHERE user1=? AND user2=?
        """, (a, b)).fetchone()

        if existing:
            return existing

        cur = conn.execute("""
            INSERT INTO matches(user1, user2, created_at)
            VALUES (?, ?, ?)
        """, (a, b, now_iso()))

        match_id = cur.lastrowid
        return conn.execute(
            "SELECT * FROM matches WHERE id=?",
            (match_id,),
        ).fetchone()


def swipe_and_match(user_id, target_id, action):
    save_swipe(user_id, target_id, action)

    if action not in ("like", "super"):
        return None

    other = get_swipe(target_id, user_id)

    if other and other["action"] in ("like", "super"):
        return create_match_if_needed(user_id, target_id)

    return None


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard(is_admin=False):
    rows = [
        [
            InlineKeyboardButton(
                "👤 Իմ պրոֆիլը",
                callback_data="profile",
            ),
            InlineKeyboardButton(
                "🔎 Գտնել մարդկանց",
                callback_data="discover",
            ),
        ],
        [
            InlineKeyboardButton(
                "❤️ Իմ Match-երը",
                callback_data="matches",
            ),
            InlineKeyboardButton(
                "✏️ Խմբագրել պրոֆիլը",
                callback_data="edit",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚫 Բլոկավորվածներ",
                callback_data="blocked_list",
            ),
            InlineKeyboardButton(
                "⚙️ Կարգավորումներ",
                callback_data="settings",
            ),
        ],
    ]

    if is_admin:
        rows.append([
            InlineKeyboardButton(
                "🛡️ Admin մենյու",
                callback_data="admin:menu",
            )
        ])

    return InlineKeyboardMarkup(rows)


def reply_main_keyboard(is_admin=False):
    rows = [
        ["👤 Իմ պրոֆիլը", "🔎 Գտնել մարդկանց"],
        ["❤️ Իմ Match-երը", "✏️ Խմբագրել պրոֆիլը"],
        ["🚫 Բլոկավորվածներ", "⚙️ Կարգավորումներ"],
    ]

    if is_admin:
        rows.append(["🛡️ Admin մենյու"])

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def gender_keyboard():
    return ReplyKeyboardMarkup(
        [["👨 Տղամարդ", "👩 Կին"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def looking_keyboard():
    return ReplyKeyboardMarkup(
        [["👨 Տղամարդ", "👩 Կին"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def discovery_keyboard(target_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Բաց թողնել",
                callback_data=f"pass:{target_id}",
            ),
            InlineKeyboardButton(
                "❤️ Հավանել",
                callback_data=f"like:{target_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⭐ Super Like",
                callback_data=f"super:{target_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚨 Բողոքել",
                callback_data=f"report_menu:{target_id}",
            ),
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            ),
        ],
    ])


def match_keyboard(match_id, other_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💬 Բացել չատը",
                callback_data=f"chat:{match_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🚫 Բլոկավորել",
                callback_data=f"block:{other_id}",
            ),
            InlineKeyboardButton(
                "🚨 Բողոքել",
                callback_data=f"report_menu:{other_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔎 Գտնել մարդկանց",
                callback_data="discover",
            ),
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            ),
        ],
    ])


def admin_keyboard():
    enabled = activity_notifications_enabled()
    status = "🟢 Միացված" if enabled else "🔴 Անջատված"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 Վիճակագրություն",
                callback_data="admin:stats",
            ),
            InlineKeyboardButton(
                "👥 Օգտատերեր",
                callback_data="admin:users",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚨 Հաղորդումներ",
                callback_data="admin:reports",
            ),
        ],
        [
            InlineKeyboardButton(
                f"🔔 Գործողությունների ծանուցումներ՝ {status}",
                callback_data="admin:activity_toggle",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎵 TikTok վիճակագրություն",
                callback_data="admin:tiktok",
            ),
        ],
        [
            InlineKeyboardButton(
                "🏠 Գլխավոր մենյու",
                callback_data="home",
            ),
        ],
    ])


def report_keyboard(target_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤬 Վիրավորանք / անպատշաճ վարք",
                callback_data=f"report:{target_id}:behavior",
            )
        ],
        [
            InlineKeyboardButton(
                "🚫 Կեղծ պրոֆիլ",
                callback_data=f"report:{target_id}:fake",
            )
        ],
        [
            InlineKeyboardButton(
                "🔞 Անպատշաճ բովանդակություն",
                callback_data=f"report:{target_id}:content",
            )
        ],
        [
            InlineKeyboardButton(
                "⚠️ Այլ",
                callback_data=f"report:{target_id}:other",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Հետ",
                callback_data="discover",
            )
        ],
    ])


def settings_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🗑️ Ջնջել իմ պրոֆիլը",
                callback_data="delete_profile",
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            )
        ],
    ])


# ============================================================
# TEXT RENDERING
# ============================================================

def profile_text(user, title="👤 Պրոֆիլ"):
    return (
        f"<b>{title}</b>\n\n"
        f"👤 Անուն՝ <b>{html.escape(str(user['name'] or '—'))}</b>\n"
        f"🎂 Տարիք՝ <b>{html.escape(str(user['age'] or '—'))}</b>\n"
        f"📍 Քաղաք՝ <b>{html.escape(str(user['city'] or '—'))}</b>\n"
        f"⚧ Սեռ՝ <b>{html.escape(str(user['gender'] or '—'))}</b>\n"
        f"❤️ Փնտրում է՝ <b>{html.escape(str(user['looking_for'] or '—'))}</b>\n\n"
        f"📝 <b>Իմ մասին</b>\n"
        f"{html.escape(str(user['about'] or '—'))}"
    )


# ============================================================
# HOME / START
# ============================================================

async def home(update, context, edit=False):
    user = get_user(update.effective_user.id)

    if not user:
        ensure_user(update.effective_user)
        user = get_user(update.effective_user.id)

    if user and user["banned"]:
        text = "🚫 <b>Քո հաշիվը արգելափակված է։</b>"
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML")
        else:
            await send_ui_message(update, context, text, parse_mode="HTML")
        return

    text = (
        "💙 <b>Բարի գալուստ Together</b>\n\n"
        "Այստեղ կարող ես գտնել նոր մարդկանց, "
        "ստեղծել Match և սկսել շփվել։\n\n"
        "Ընտրիր գործողությունը 👇"
    )

    markup = main_keyboard(update.effective_user.id == ADMIN_ID)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception:
            await send_ui_message(update, context, 
                text,
                parse_mode="HTML",
                reply_markup=markup,
            )
    else:
        await send_ui_message(update, context, 
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    if is_banned(user.id):
        await send_ui_message(update, context, 
            "🚫 <b>Քո հաշիվը արգելափակված է։</b>",
            parse_mode="HTML",
        )
        return

    touch(user.id)

    # --------------------------------------------------------
    # TikTok → Together attribution
    # Example:
    # https://t.me/BOT_USERNAME?start=tiktok
    # --------------------------------------------------------
    source = None

    if context.args:
        source_candidate = str(context.args[0]).strip().lower()

        if source_candidate == "tiktok":
            source = "tiktok"

    if source:
        saved = set_acquisition_source(user.id, source)

        if saved:
            await log_activity(
                user.id,
                "source:tiktok",
                "Օգտատերը մուտք գործեց TikTok deep-link-ից",
                context=context,
                notify=False,
            )

            await notify_admin(
                context,
                "🎵 <b>Նոր TikTok օգտատեր</b>\n\n"
                f"👤 ID՝ <code>{user.id}</code>\n"
                f"🔗 Source՝ <b>TikTok</b>",
            )

    context.user_data.clear()

    await log_activity(
        user.id,
        "start",
        context=context,
        notify=False,
    )

    current = get_user(user.id)

    if profile_complete(current):
        await home(update, context)
        return

    context.user_data["editing"] = True
    context.user_data["step"] = "name"

    await send_ui_message(update, context, 
        "👋 Բարի գալուստ Together։\n\n"
        "Սկսենք քո պրոֆիլից։\n\n"
        "✏️ Գրիր քո անունը։",
        reply_markup=ReplyKeyboardRemove(),
    )


# ============================================================
# PROFILE CREATION / EDITING
# ============================================================

async def begin_profile(update, context, editing=True):
    touch(update.effective_user.id)
    context.user_data.clear()
    context.user_data["editing"] = editing
    context.user_data["step"] = "name"

    await send_ui_message(update, context, 
        "✏️ <b>Քայլ 1/7</b>\n\n"
        "Գրիր քո անունը։",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_profile_text(update, context):
    user_id = update.effective_user.id
    touch(user_id)

    step = context.user_data.get("step")

    if not step:
        return False

    text = (update.effective_message.text or "").strip()

    if step == "name":
        if len(text) < 2 or len(text) > 40:
            await send_ui_message(update, context, 
                "❗ Անունը պետք է լինի 2–40 նիշ։"
            )
            return True

        update_user(user_id, name=text)
        context.user_data["step"] = "age"

        await send_ui_message(update, context, 
            "🎂 <b>Քայլ 2/7</b>\n\n"
            "Գրիր քո տարիքը (18–99)։",
            parse_mode="HTML",
        )
        return True

    if step == "age":
        try:
            age = int(text)
        except ValueError:
            await send_ui_message(update, context, 
                "❗ Տարիքը գրիր թվերով։ Օրինակ՝ 25"
            )
            return True

        if not 18 <= age <= 99:
            await send_ui_message(update, context, 
                "❗ Տարիքը պետք է լինի 18–99։"
            )
            return True

        update_user(user_id, age=age)
        context.user_data["step"] = "city"

        await send_ui_message(update, context, 
            "📍 <b>Քայլ 3/7</b>\n\n"
            "Ո՞ր քաղաքում ես ապրում։",
            parse_mode="HTML",
        )
        return True

    if step == "city":
        if len(text) < 2 or len(text) > 60:
            await send_ui_message(update, context, 
                "❗ Գրիր ճիշտ քաղաքի անունը։"
            )
            return True

        update_user(user_id, city=text)
        context.user_data["step"] = "gender"

        await send_ui_message(update, context, 
            "⚧ <b>Քայլ 4/7</b>\n\n"
            "Ընտրիր քո սեռը։",
            parse_mode="HTML",
            reply_markup=gender_keyboard(),
        )
        return True

    if step == "gender":
        mapping = {
            "👨 Տղամարդ": "Տղամարդ",
            "👩 Կին": "Կին",
        }

        gender = mapping.get(text)

        if not gender:
            await send_ui_message(update, context, 
                "❗ Ընտրիր տարբերակներից մեկը։",
                reply_markup=gender_keyboard(),
            )
            return True

        update_user(user_id, gender=gender)
        context.user_data["step"] = "looking_for"

        await send_ui_message(update, context, 
            "❤️ <b>Քայլ 5/7</b>\n\n"
            "Ո՞ւմ ես փնտրում։",
            parse_mode="HTML",
            reply_markup=looking_keyboard(),
        )
        return True

    if step == "looking_for":
        mapping = {
            "👨 Տղամարդ": "Տղամարդ",
            "👩 Կին": "Կին",
        }

        looking = mapping.get(text)

        if not looking:
            await send_ui_message(update, context, 
                "❗ Ընտրիր տարբերակներից մեկը։",
                reply_markup=looking_keyboard(),
            )
            return True

        update_user(user_id, looking_for=looking)
        context.user_data["step"] = "about"

        await send_ui_message(update, context, 
            "📝 <b>Քայլ 6/7</b>\n\n"
            "Մի փոքր պատմիր քո մասին։",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return True

    if step == "about":
        if len(text) < 2 or len(text) > 1000:
            await send_ui_message(update, context, 
                "❗ «Իմ մասին» բաժինը պետք է լինի 2–1000 նիշ։"
            )
            return True

        update_user(user_id, about=text)
        context.user_data["step"] = "photo"

        await send_ui_message(update, context, 
            "📸 <b>Քայլ 7/7</b>\n\n"
            "Ուղարկիր քո լուսանկարը։\n\n"
            "Լուսանկարը պարտադիր է պրոֆիլը ավարտելու համար։",
            parse_mode="HTML",
        )
        return True

    return False


async def handle_profile_photo(update, context):
    if context.user_data.get("step") != "photo":
        return False

    user_id = update.effective_user.id
    touch(user_id)

    photo = update.effective_message.photo

    if not photo:
        return False

    file_id = photo[-1].file_id
    update_user(user_id, photo_file_id=file_id)

    context.user_data.clear()

    await log_activity(
        user_id,
        "photo_updated",
        "Պրոֆիլի լուսանկարը թարմացվեց",
        context=context,
        notify=False,
    )

    user = get_user(user_id)

    if profile_complete(user):
        await log_activity(
            user_id,
            "profile_completed",
            context=context,
            notify=False,
        )

        await send_ui_message(update, context, 
            "✅ <b>Պրոֆիլդ պատրաստ է։</b>\n\n"
            "Բարի գալուստ Together 💙",
            parse_mode="HTML",
            reply_markup=reply_main_keyboard(
                user_id == ADMIN_ID
            ),
        )

        await send_ui_message(update, context, 
            "🏠 Ընտրիր գործողությունը 👇",
            reply_markup=main_keyboard(user_id == ADMIN_ID),
        )
    else:
        await send_ui_message(update, context, 
            "⚠️ Պրոֆիլը դեռ ամբողջական չէ։\n"
            "Փորձիր կրկին խմբագրել պրոֆիլը։",
            reply_markup=main_keyboard(user_id == ADMIN_ID),
        )

    return True


# ============================================================
# PROFILE VIEW
# ============================================================

async def show_profile(update, context, user_id=None, own=False):
    viewer_id = update.effective_user.id
    target_id = viewer_id if own or user_id is None else user_id

    user = get_user(target_id)

    if not user:
        await send_ui_message(update, context, 
            "❗ Պրոֆիլը չի գտնվել։"
        )
        return

    if not own:
        await log_activity(
            viewer_id,
            "profile_viewed",
            f"Դիտվել է օգտատեր {target_id}-ի պրոֆիլը",
            context=context,
            notify=False,
        )

    text = profile_text(
        user,
        "👤 Իմ պրոֆիլը" if own else "👤 Պրոֆիլ",
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✏️ Խմբագրել",
                callback_data="edit",
            ),
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            ),
        ]
    ])

    if user["photo_file_id"]:
        try:
            await update.effective_message.reply_photo(
                photo=user["photo_file_id"],
                caption=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except Exception:
            pass

    await send_ui_message(update, context, 
        text,
        parse_mode="HTML",
        reply_markup=markup,
    )


# ============================================================
# DISCOVERY
# ============================================================

async def show_next_profile(update, context):
    user_id = update.effective_user.id
    touch(user_id)

    candidate = get_next_profile(user_id)

    if not candidate:
        text = (
            "🔎 <b>Նոր պրոֆիլներ այս պահին չկան։</b>\n\n"
            "Փորձիր ավելի ուշ։"
        )

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔄 Կրկին փորձել",
                    callback_data="discover",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Գլխավոր",
                    callback_data="home",
                )
            ],
        ])

        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            except Exception:
                await send_ui_message(update, context, 
                    text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
        else:
            await send_ui_message(update, context, 
                text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        return

    text = profile_text(candidate, "🔎 Հնարավոր Match")

    if update.callback_query:
        try:
            await update.callback_query.delete_message()
        except Exception:
            pass

    await log_activity(
        user_id,
        "profile_viewed",
        f"Discovery-ում ցուցադրվեց {candidate['id']}",
        context=context,
        notify=False,
    )

    try:
        await send_ui_photo(update, context,
            photo=candidate["photo_file_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=discovery_keyboard(candidate["id"]),
        )
    except Exception:
        await send_ui_chat_message(update, context,
            text=text,
            parse_mode="HTML",
            reply_markup=discovery_keyboard(candidate["id"]),
        )


async def process_swipe(update, context, action, target_id):
    user_id = update.effective_user.id

    try:
        target_id = int(target_id)
    except ValueError:
        return

    if user_id == target_id:
        return

    if is_banned(user_id):
        await update.callback_query.answer(
            "Քո հաշիվը արգելափակված է։",
            show_alert=True,
        )
        return

    target = get_user(target_id)

    if not target or target["banned"]:
        await update.callback_query.answer(
            "Այս պրոֆիլը հասանելի չէ։",
            show_alert=True,
        )
        await show_next_profile(update, context)
        return

    if are_blocked_either_way(user_id, target_id):
        await update.callback_query.answer(
            "Այս պրոֆիլը հասանելի չէ։",
            show_alert=True,
        )
        await show_next_profile(update, context)
        return

    match = swipe_and_match(user_id, target_id, action)

    labels = {
        "pass": "Բաց թողնվեց",
        "like": "Հավանեցիր ❤️",
        "super": "Super Like ուղարկվեց ⭐",
    }

    await log_activity(
        user_id,
        f"swipe:{action}",
        f"Թիրախ՝ {target_id}",
        context=context,
        notify=False,
    )

    if match:
        other = get_user(target_id)

        await update.callback_query.answer(
            "🎉 Match!",
            show_alert=True,
        )

        try:
            await update.callback_query.delete_message()
        except Exception:
            pass

        await send_ui_chat_message(update, context,
            "🎉 <b>Դուք Match եք!</b>\n\n"
            f"❤️ Դու և <b>{html.escape(str(other['name']))}</b> "
            "հավանել եք միմյանց։\n\n"
            "Սկսիր զրույցը 👇",
            parse_mode="HTML",
            reply_markup=match_keyboard(match["id"], target_id),
        )

        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "🎉 <b>Նոր Match!</b>\n\n"
                    f"❤️ <b>{html.escape(str(get_user(user_id)['name']))}</b> "
                    "նույնպես հավանել է քեզ։\n\n"
                    "Բացիր չատը 👇"
                ),
                parse_mode="HTML",
                reply_markup=match_keyboard(match["id"], user_id),
            )
        except Exception as exc:
            logger.info("Could not notify matched user: %s", exc)

        await log_activity(
            user_id,
            "match_created",
            f"Match ID՝ {match['id']}, օգտատեր՝ {target_id}",
            context=context,
            notify=True,
        )
        return

    await update.callback_query.answer(labels.get(action, "Պատրաստ է"))
    await show_next_profile(update, context)


# ============================================================
# MATCHES
# ============================================================

async def show_matches(update, context):
    user_id = update.effective_user.id
    touch(user_id)

    rows = get_user_matches(user_id)

    if not rows:
        text = (
            "❤️ <b>Իմ Match-երը</b>\n\n"
            "Դեռ Match չունես։\n"
            "Գնա «🔎 Գտնել մարդկանց» և սկսիր։"
        )

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔎 Գտնել մարդկանց",
                    callback_data="discover",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Գլխավոր",
                    callback_data="home",
                )
            ],
        ])

        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        else:
            await send_ui_message(update, context, 
                text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        return

    buttons = []

    for row in rows:
        other = get_user(row["other_user_id"])
        if not other:
            continue

        buttons.append([
            InlineKeyboardButton(
                f"❤️ {other['name']} · {other['age']}",
                callback_data=f"chat:{row['id']}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🏠 Գլխավոր",
            callback_data="home",
        )
    ])

    markup = InlineKeyboardMarkup(buttons)

    text = (
        "❤️ <b>Իմ Match-երը</b>\n\n"
        "Ընտրիր Match-ը՝ զրույցը բացելու համար։"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    else:
        await send_ui_message(update, context, 
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )


# ============================================================
# CHAT
# ============================================================

def get_other_in_match(match, user_id):
    if not match:
        return None
    return (
        match["user2"]
        if match["user1"] == user_id
        else match["user1"]
    )


async def open_chat(update, context, match_id):
    user_id = update.effective_user.id
    match = get_match(match_id)

    if not match:
        await update.callback_query.answer(
            "Match-ը չի գտնվել։",
            show_alert=True,
        )
        return

    if user_id not in (match["user1"], match["user2"]):
        await update.callback_query.answer(
            "Դու այս Match-ի մասնակից չես։",
            show_alert=True,
        )
        return

    other_id = get_other_in_match(match, user_id)
    other = get_user(other_id)

    context.user_data["chat_match_id"] = match_id
    touch(user_id)

    try:
        await update.callback_query.edit_message_text(
            "💬 <b>Չատ</b>\n\n"
            f"❤️ Զրուցում ես <b>{html.escape(str(other['name']))}</b>-ի հետ։\n\n"
            "Ուղարկիր հաղորդագրություն։\n"
            "⏱️ Եթե 3 րոպե ոչ ոք չգրի, չատը ավտոմատ կփակվի։\n\n"
            "Match-ը կմնա պահպանված։",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🚫 Բլոկավորել",
                        callback_data=f"block:{other_id}",
                    ),
                    InlineKeyboardButton(
                        "🚨 Բողոքել",
                        callback_data=f"report_menu:{other_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "❤️ Իմ Match-երը",
                        callback_data="matches",
                    ),
                    InlineKeyboardButton(
                        "🏠 Գլխավոր",
                        callback_data="home",
                    ),
                ],
            ]),
        )
    except Exception:
        await send_ui_message(update, context, 
            "💬 Չատը բացված է։",
            parse_mode="HTML",
        )


async def handle_chat_message(update, context):
    match_id = context.user_data.get("chat_match_id")

    if not match_id:
        return False

    user_id = update.effective_user.id
    touch(user_id)

    match = get_match(match_id)

    if not match or user_id not in (
        match["user1"],
        match["user2"],
    ):
        context.user_data.pop("chat_match_id", None)
        return False

    text = (update.effective_message.text or "").strip()

    if not text:
        return True

    if len(text) > 4000:
        await send_ui_message(update, context, 
            "❗ Հաղորդագրությունը շատ երկար է։"
        )
        return True

    with db() as conn:
        conn.execute("""
            INSERT INTO messages(
                match_id, sender_id, text, created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            match_id,
            user_id,
            text,
            now_iso(),
        ))

    other_id = get_other_in_match(match, user_id)

    try:
        await context.bot.send_message(
            chat_id=other_id,
            text=(
                f"💬 <b>Նոր հաղորդագրություն</b>\n\n"
                f"{html.escape(text)}"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.info("Could not deliver chat message: %s", exc)

    await log_activity(
        user_id,
        "chat_message",
        f"Match ID՝ {match_id}",
        context=context,
        notify=False,
    )

    return True


# ============================================================
# REPORTS
# ============================================================

REPORT_REASONS = {
    "behavior": "Վիրավորանք / անպատշաճ վարք",
    "fake": "Կեղծ պրոֆիլ",
    "content": "Անպատշաճ բովանդակություն",
    "other": "Այլ",
}


def add_report(reporter, reported, reason):
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO reports(
                reporter, reported, reason, status, created_at
            )
            VALUES (?, ?, ?, 'open', ?)
        """, (
            reporter,
            reported,
            reason,
            now_iso(),
        ))
        return cur.lastrowid


async def report_menu(update, context, target_id):
    try:
        target_id = int(target_id)
    except ValueError:
        return

    await update.callback_query.edit_message_text(
        "🚨 <b>Բողոք</b>\n\n"
        "Ընտրիր պատճառը։",
        parse_mode="HTML",
        reply_markup=report_keyboard(target_id),
    )


async def submit_report(update, context, target_id, reason_key):
    reporter = update.effective_user.id

    try:
        target_id = int(target_id)
    except ValueError:
        await update.callback_query.answer(
            "Սխալ պրոֆիլ։",
            show_alert=True,
        )
        return

    reason = REPORT_REASONS.get(reason_key, "Այլ")

    report_id = add_report(
        reporter,
        target_id,
        reason,
    )

    await update.callback_query.answer(
        "Բողոքը ուղարկվեց։",
        show_alert=True,
    )

    await log_activity(
        reporter,
        "report",
        f"Report ID՝ {report_id}, reported՝ {target_id}, reason՝ {reason}",
        context=context,
        notify=False,
    )

    await notify_admin(
        context,
        "🚨 <b>Նոր հաղորդում</b>\n\n"
        f"🆔 Report՝ <code>{report_id}</code>\n"
        f"👤 Reporter՝ <code>{reporter}</code>\n"
        f"🎯 Reported՝ <code>{target_id}</code>\n"
        f"📝 Պատճառ՝ <b>{html.escape(reason)}</b>",
    )

    await update.callback_query.edit_message_text(
        "✅ <b>Բողոքը ընդունվեց։</b>\n\n"
        "Շնորհակալություն տեղեկացնելու համար։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🏠 Գլխավոր",
                    callback_data="home",
                )
            ]
        ]),
    )


# ============================================================
# BLOCKS
# ============================================================

async def block_user(update, context, target_id):
    user_id = update.effective_user.id

    try:
        target_id = int(target_id)
    except ValueError:
        return

    if target_id == user_id:
        return

    add_block(user_id, target_id)

    context.user_data.pop("chat_match_id", None)

    await update.callback_query.answer(
        "Օգտատերը բլոկավորվեց։",
        show_alert=True,
    )

    await log_activity(
        user_id,
        "block",
        f"Blocked՝ {target_id}",
        context=context,
        notify=False,
    )

    await update.callback_query.edit_message_text(
        "🚫 <b>Օգտատերը բլոկավորված է։</b>\n\n"
        "Նրա պրոֆիլը այլևս չի ցուցադրվի քեզ։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🚫 Բլոկավորվածներ",
                    callback_data="blocked_list",
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


async def blocked_list(update, context):
    user_id = update.effective_user.id
    touch(user_id)

    rows = get_blocked_users(user_id)

    if not rows:
        text = (
            "🚫 <b>Բլոկավորվածներ</b>\n\n"
            "Բլոկավորված օգտատերեր չկան։"
        )

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🏠 Գլխավոր",
                    callback_data="home",
                )
            ]
        ])

        await update.callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return

    buttons = []

    for row in rows:
        buttons.append([
            InlineKeyboardButton(
                f"🚫 {row['name']}",
                callback_data=f"unblock:{row['id']}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🏠 Գլխավոր",
            callback_data="home",
        )
    ])

    await update.callback_query.edit_message_text(
        "🚫 <b>Բլոկավորվածներ</b>\n\n"
        "Ընտրիր օգտատիրոջը՝ բլոկը հանելու համար։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def unblock_user(update, context, target_id):
    user_id = update.effective_user.id

    try:
        target_id = int(target_id)
    except ValueError:
        return

    remove_block(user_id, target_id)

    await update.callback_query.answer(
        "Բլոկը հանվեց։",
        show_alert=True,
    )

    await log_activity(
        user_id,
        "unblock",
        f"Unblocked՝ {target_id}",
        context=context,
        notify=False,
    )

    await blocked_list(update, context)


# ============================================================
# DELETE PROFILE
# ============================================================

async def delete_profile_menu(update, context):
    await update.callback_query.edit_message_text(
        "🗑️ <b>Ջնջե՞լ պրոֆիլը</b>\n\n"
        "Այս գործողությունը կջնջի քո պրոֆիլի տվյալները "
        "և կապված տվյալները։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❌ Այո, ջնջել",
                    callback_data="delete_yes",
                ),
                InlineKeyboardButton(
                    "⬅️ Չեղարկել",
                    callback_data="settings",
                ),
            ]
        ]),
    )


async def delete_profile_confirm(update, context):
    user_id = update.effective_user.id

    with db() as conn:
        conn.execute("DELETE FROM swipes WHERE from_user=? OR to_user=?", (user_id, user_id))
        conn.execute("DELETE FROM messages WHERE sender_id=?", (user_id,))
        conn.execute("DELETE FROM reports WHERE reporter=? OR reported=?", (user_id, user_id))
        conn.execute("DELETE FROM blocks WHERE blocker=? OR blocked=?", (user_id, user_id))
        conn.execute("DELETE FROM matches WHERE user1=? OR user2=?", (user_id, user_id))
        conn.execute("DELETE FROM activity_logs WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    context.user_data.clear()

    await update.callback_query.answer(
        "Պրոֆիլը ջնջվեց։",
        show_alert=True,
    )

    await update.callback_query.edit_message_text(
        "🗑️ <b>Պրոֆիլը ջնջված է։</b>\n\n"
        "Եթե ցանկանում ես, կարող ես նորից սկսել՝ /start",
        parse_mode="HTML",
    )


# ============================================================
# SETTINGS
# ============================================================

async def show_settings(update, context):
    await update.callback_query.edit_message_text(
        "⚙️ <b>Կարգավորումներ</b>\n\n"
        "Այստեղ կարող ես կառավարել քո պրոֆիլը։",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


# ============================================================
# ADMIN
# ============================================================

def admin_required(user_id):
    return ADMIN_ID and user_id == ADMIN_ID


async def admin_menu(update, context):
    user_id = update.effective_user.id

    if not admin_required(user_id):
        await update.callback_query.answer(
            "Մուտքը թույլատրված չէ։",
            show_alert=True,
        )
        return

    await update.callback_query.edit_message_text(
        "🛡️ <b>Admin մենյու</b>\n\n"
        "Ընտրիր բաժինը։",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


async def admin_stats(update, context):
    if not admin_required(update.effective_user.id):
        return

    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        active_24h = conn.execute("""
            SELECT COUNT(*) c
            FROM users
            WHERE last_active >= ?
        """, (
            (utc_now() - timedelta(hours=24)).isoformat(),
        )).fetchone()["c"]

        active_7d = conn.execute("""
            SELECT COUNT(*) c
            FROM users
            WHERE last_active >= ?
        """, (
            (utc_now() - timedelta(days=7)).isoformat(),
        )).fetchone()["c"]

        complete = conn.execute("""
            SELECT COUNT(*) c
            FROM users
            WHERE name IS NOT NULL AND name != ''
              AND age IS NOT NULL
              AND city IS NOT NULL AND city != ''
              AND gender IS NOT NULL AND gender != ''
              AND looking_for IS NOT NULL AND looking_for != ''
              AND about IS NOT NULL AND about != ''
              AND photo_file_id IS NOT NULL AND photo_file_id != ''
        """).fetchone()["c"]

        banned = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE banned=1"
        ).fetchone()["c"]

        likes = conn.execute("""
            SELECT COUNT(*) c
            FROM swipes
            WHERE action='like'
        """).fetchone()["c"]

        superlikes = conn.execute("""
            SELECT COUNT(*) c
            FROM swipes
            WHERE action='super'
        """).fetchone()["c"]

        passes = conn.execute("""
            SELECT COUNT(*) c
            FROM swipes
            WHERE action='pass'
        """).fetchone()["c"]

        matches = conn.execute(
            "SELECT COUNT(*) c FROM matches"
        ).fetchone()["c"]

        messages = conn.execute(
            "SELECT COUNT(*) c FROM messages"
        ).fetchone()["c"]

        reports = conn.execute("""
            SELECT COUNT(*) c
            FROM reports
            WHERE status='open'
        """).fetchone()["c"]

        views = conn.execute("""
            SELECT COUNT(*) c
            FROM activity_logs
            WHERE action='profile_viewed'
        """).fetchone()["c"]

    text = (
        "📊 <b>Ընդհանուր վիճակագրություն</b>\n\n"
        f"👥 Ընդհանուր օգտատերեր՝ <b>{total}</b>\n"
        f"🟢 Ակտիվ 24 ժամում՝ <b>{active_24h}</b>\n"
        f"📅 Ակտիվ 7 օրում՝ <b>{active_7d}</b>\n"
        f"✅ Ավարտված պրոֆիլներ՝ <b>{complete}</b>\n"
        f"🚫 Բլոկավորված՝ <b>{banned}</b>\n\n"
        f"❤️ Likes՝ <b>{likes}</b>\n"
        f"⭐ Super Likes՝ <b>{superlikes}</b>\n"
        f"❌ Pass՝ <b>{passes}</b>\n"
        f"❤️ Matches՝ <b>{matches}</b>\n"
        f"💬 Հաղորդագրություններ՝ <b>{messages}</b>\n"
        f"👁️ Պրոֆիլի դիտումներ՝ <b>{views}</b>\n"
        f"🚨 Բաց հաղորդումներ՝ <b>{reports}</b>"
    )

    await update.callback_query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Admin մենյու",
                    callback_data="admin:menu",
                )
            ]
        ]),
    )


async def admin_tiktok_stats(update, context):
    if not admin_required(update.effective_user.id):
        return

    now = utc_now()
    today_start = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    week_start = now - timedelta(days=7)
    active_start = now - timedelta(hours=24)

    with db() as conn:
        total = conn.execute("""
            SELECT COUNT(*) c
            FROM users
            WHERE acquisition_source='tiktok'
        """).fetchone()["c"]

        today = conn.execute("""
            SELECT COUNT(*) c
            FROM users
            WHERE acquisition_source='tiktok'
              AND created_at >= ?
        """, (
            today_start.isoformat(),
        )).fetchone()["c"]

        last_7d = conn.execute("""
            SELECT COUNT(*) c
            FROM users
            WHERE acquisition_source='tiktok'
              AND created_at >= ?
        """, (
            week_start.isoformat(),
        )).fetchone()["c"]

        active_24h = conn.execute("""
            SELECT COUNT(*) c
            FROM users
            WHERE acquisition_source='tiktok'
              AND last_active >= ?
        """, (
            active_start.isoformat(),
        )).fetchone()["c"]

        complete = conn.execute("""
            SELECT COUNT(*) c
            FROM users
            WHERE acquisition_source='tiktok'
              AND name IS NOT NULL AND name != ''
              AND age IS NOT NULL
              AND city IS NOT NULL AND city != ''
              AND gender IS NOT NULL AND gender != ''
              AND looking_for IS NOT NULL AND looking_for != ''
              AND about IS NOT NULL AND about != ''
              AND photo_file_id IS NOT NULL AND photo_file_id != ''
        """).fetchone()["c"]

        matches = conn.execute("""
            SELECT COUNT(DISTINCT m.id) c
            FROM matches m
            JOIN users u1 ON u1.id=m.user1
            JOIN users u2 ON u2.id=m.user2
            WHERE u1.acquisition_source='tiktok'
               OR u2.acquisition_source='tiktok'
        """).fetchone()["c"]

        likes_received = conn.execute("""
            SELECT COUNT(*) c
            FROM swipes s
            JOIN users u ON u.id=s.to_user
            WHERE s.action IN ('like','super')
              AND u.acquisition_source='tiktok'
        """).fetchone()["c"]

        likes_sent = conn.execute("""
            SELECT COUNT(*) c
            FROM swipes s
            JOIN users u ON u.id=s.from_user
            WHERE s.action IN ('like','super')
              AND u.acquisition_source='tiktok'
        """).fetchone()["c"]

    completion_rate = (
        (complete / total) * 100
        if total else 0
    )

    text = (
        "🎵 <b>TikTok → Together վիճակագրություն</b>\n\n"
        f"👥 TikTok-ից մուտք գործած օգտատերեր՝ <b>{total}</b>\n"
        f"📅 Այսօր՝ <b>{today}</b>\n"
        f"📆 Վերջին 7 օրում՝ <b>{last_7d}</b>\n"
        f"🟢 Ակտիվ վերջին 24 ժամում՝ <b>{active_24h}</b>\n\n"
        f"✅ Ավարտված պրոֆիլներ՝ <b>{complete}</b>\n"
        f"📈 Պրոֆիլի ավարտման տոկոս՝ <b>{completion_rate:.1f}%</b>\n\n"
        f"❤️ TikTok օգտատերերի ուղարկած Like/Super Like՝ <b>{likes_sent}</b>\n"
        f"💗 TikTok օգտատերերի ստացած Like/Super Like՝ <b>{likes_received}</b>\n"
        f"🎉 TikTok օգտատերերի մասնակցությամբ Match-եր՝ <b>{matches}</b>"
    )

    await update.callback_query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Admin մենյու",
                    callback_data="admin:menu",
                )
            ]
        ]),
    )


async def admin_users(update, context):
    if not admin_required(update.effective_user.id):
        return

    with db() as conn:
        rows = conn.execute("""
            SELECT id, username, name, age, city,
                   banned, created_at, last_active,
                   acquisition_source
            FROM users
            ORDER BY created_at DESC
            LIMIT 30
        """).fetchall()

    if not rows:
        text = "👥 Օգտատերեր չկան։"
    else:
        lines = ["👥 <b>Վերջին օգտատերերը</b>\n"]

        for row in rows:
            username = (
                f"@{row['username']}"
                if row["username"]
                else "—"
            )
            status = "🚫" if row["banned"] else "🟢"
            source = row["acquisition_source"] or "direct"

            lines.append(
                f"{status} <code>{row['id']}</code> "
                f"{html.escape(str(row['name'] or '—'))} "
                f"({html.escape(str(row['age'] or '—'))})\n"
                f"   {html.escape(username)} · "
                f"{html.escape(str(row['city'] or '—'))} · "
                f"source: <b>{html.escape(source)}</b>"
            )

        text = "\n".join(lines)

    await update.callback_query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Admin մենյու",
                    callback_data="admin:menu",
                )
            ]
        ]),
    )


async def admin_reports(update, context):
    if not admin_required(update.effective_user.id):
        return

    with db() as conn:
        rows = conn.execute("""
            SELECT *
            FROM reports
            WHERE status='open'
            ORDER BY id DESC
            LIMIT 30
        """).fetchall()

    if not rows:
        text = "🚨 <b>Բաց հաղորդումներ չկան։</b>"
    else:
        lines = ["🚨 <b>Բաց հաղորդումներ</b>\n"]

        for row in rows:
            lines.append(
                f"🆔 <code>{row['id']}</code>\n"
                f"👤 Reporter՝ <code>{row['reporter']}</code>\n"
                f"🎯 Reported՝ <code>{row['reported']}</code>\n"
                f"📝 {html.escape(row['reason'])}\n"
                f"🕒 {fmt_time(row['created_at'])}\n"
            )

        text = "\n".join(lines)

    await update.callback_query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Admin մենյու",
                    callback_data="admin:menu",
                )
            ]
        ]),
    )


async def admin_activity_toggle(update, context):
    if not admin_required(update.effective_user.id):
        return

    current = activity_notifications_enabled()
    set_setting("activity_notifications", "0" if current else "1")

    status = (
        "🟢 միացված"
        if not current
        else "🔴 անջատված"
    )

    await update.callback_query.answer(
        f"Գործողությունների ծանուցումները {status}",
        show_alert=True,
    )

    await admin_menu(update, context)


async def admin_callback(update, context, action):
    if not admin_required(update.effective_user.id):
        await update.callback_query.answer(
            "Մուտքը թույլատրված չէ։",
            show_alert=True,
        )
        return

    if action == "menu":
        await admin_menu(update, context)
    elif action == "stats":
        await admin_stats(update, context)
    elif action == "tiktok":
        await admin_tiktok_stats(update, context)
    elif action == "users":
        await admin_users(update, context)
    elif action == "reports":
        await admin_reports(update, context)
    elif action == "activity_toggle":
        await admin_activity_toggle(update, context)


# ============================================================
# 3-MINUTE BOT UI AUTO-CLEANUP
# ============================================================

async def inactivity_cleanup(context):
    """Delete temporary bot UI messages after 3 minutes.

    This does NOT close Match chats and does NOT delete Match messages.
    """
    await cleanup_ui_messages(context)


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    track_callback_ui(context, update)
    await query.answer()

    user_id = update.effective_user.id
    ensure_user(update.effective_user)
    touch(user_id)

    data = query.data or ""

    # --------------------------------------------------------
    # Simple navigation
    # --------------------------------------------------------
    if data == "home":
        context.user_data.pop("chat_match_id", None)
        await home(update, context, edit=True)
        return

    if data == "profile":
        await show_profile(update, context, own=True)
        return

    if data == "edit":
        await begin_profile(update, context, editing=True)
        return

    if data == "discover":
        await show_next_profile(update, context)
        return

    if data == "matches":
        context.user_data.pop("chat_match_id", None)
        await show_matches(update, context)
        return

    if data == "settings":
        await show_settings(update, context)
        return

    if data == "blocked_list":
        await blocked_list(update, context)
        return

    # --------------------------------------------------------
    # Swipe
    # --------------------------------------------------------
    if data.startswith("pass:"):
        await process_swipe(
            update,
            context,
            "pass",
            data.split(":", 1)[1],
        )
        return

    if data.startswith("like:"):
        await process_swipe(
            update,
            context,
            "like",
            data.split(":", 1)[1],
        )
        return

    if data.startswith("super:"):
        await process_swipe(
            update,
            context,
            "super",
            data.split(":", 1)[1],
        )
        return

    # --------------------------------------------------------
    # Match chat
    # --------------------------------------------------------
    if data.startswith("chat:"):
        match_id = data.split(":", 1)[1]

        try:
            match_id = int(match_id)
        except ValueError:
            return

        await open_chat(update, context, match_id)
        return

    # --------------------------------------------------------
    # Reports
    # --------------------------------------------------------
    if data.startswith("report_menu:"):
        await report_menu(
            update,
            context,
            data.split(":", 1)[1],
        )
        return

    if data.startswith("report:"):
        parts = data.split(":")

        if len(parts) >= 3:
            await submit_report(
                update,
                context,
                parts[1],
                parts[2],
            )
        return

    # --------------------------------------------------------
    # Blocks
    # --------------------------------------------------------
    if data.startswith("block:"):
        await block_user(
            update,
            context,
            data.split(":", 1)[1],
        )
        return

    if data.startswith("unblock:"):
        await unblock_user(
            update,
            context,
            data.split(":", 1)[1],
        )
        return

    # --------------------------------------------------------
    # Delete profile
    # --------------------------------------------------------
    if data == "delete_profile":
        await delete_profile_menu(update, context)
        return

    if data == "delete_yes":
        await delete_profile_confirm(update, context)
        return

    # --------------------------------------------------------
    # Admin
    # --------------------------------------------------------
    if data.startswith("admin:"):
        await admin_callback(
            update,
            context,
            data.split(":", 1)[1],
        )
        return


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    ensure_user(update.effective_user)

    if is_banned(user_id):
        await send_ui_message(update, context, 
            "🚫 <b>Քո հաշիվը արգելափակված է։</b>",
            parse_mode="HTML",
        )
        return

    touch(user_id)

    # General bot UI messages are temporary. Match chat messages are not.
    if not context.user_data.get("chat_match_id"):
        incoming = update.effective_message
        if incoming is not None:
            store = _ui_store(context.application)
            store.setdefault(user_id, {})[int(incoming.message_id)] = utc_now().timestamp()

    # Profile creation/edit flow has priority.
    if context.user_data.get("step"):
        handled = await handle_profile_text(update, context)
        if handled:
            return

    # Active chat
    if context.user_data.get("chat_match_id"):
        handled = await handle_chat_message(update, context)

        if handled:
            return

    text = (update.effective_message.text or "").strip()

    # Reply keyboard commands
    if text == "👤 Իմ պրոֆիլը":
        await show_profile(update, context, own=True)
        return

    if text == "🔎 Գտնել մարդկանց":
        await show_next_profile(update, context)
        return

    if text == "❤️ Իմ Match-երը":
        # Reuse a synthetic flow by sending a regular message.
        rows = get_user_matches(user_id)

        if not rows:
            await send_ui_message(update, context, 
                "❤️ <b>Իմ Match-երը</b>\n\n"
                "Դեռ Match չունես։",
                parse_mode="HTML",
                reply_markup=main_keyboard(user_id == ADMIN_ID),
            )
            return

        buttons = []

        for row in rows:
            other = get_user(row["other_user_id"])
            if other:
                buttons.append([
                    InlineKeyboardButton(
                        f"❤️ {other['name']} · {other['age']}",
                        callback_data=f"chat:{row['id']}",
                    )
                ])

        buttons.append([
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            )
        ])

        await send_ui_message(update, context, 
            "❤️ <b>Իմ Match-երը</b>\n\n"
            "Ընտրիր Match-ը։",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if text == "✏️ Խմբագրել պրոֆիլը":
        await begin_profile(update, context, editing=True)
        return

    if text == "🚫 Բլոկավորվածներ":
        rows = get_blocked_users(user_id)

        if not rows:
            await send_ui_message(update, context, 
                "🚫 <b>Բլոկավորվածներ</b>\n\n"
                "Բլոկավորված օգտատերեր չկան։",
                parse_mode="HTML",
                reply_markup=main_keyboard(user_id == ADMIN_ID),
            )
            return

        buttons = [
            [
                InlineKeyboardButton(
                    f"🚫 {row['name']}",
                    callback_data=f"unblock:{row['id']}",
                )
            ]
            for row in rows
        ]

        buttons.append([
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            )
        ])

        await send_ui_message(update, context, 
            "🚫 <b>Բլոկավորվածներ</b>\n\n"
            "Ընտրիր օգտատիրոջը։",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if text == "⚙️ Կարգավորումներ":
        await send_ui_message(update, context, 
            "⚙️ <b>Կարգավորումներ</b>",
            parse_mode="HTML",
            reply_markup=settings_keyboard(),
        )
        return

    if text == "🛡️ Admin մենյու" and admin_required(user_id):
        await send_ui_message(update, context, 
            "🛡️ <b>Admin մենյու</b>\n\n"
            "Ընտրիր բաժինը։",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )
        return

    await send_ui_message(update, context, 
        "👇 Ընտրիր գործողությունը մենյուից։",
        reply_markup=reply_main_keyboard(
            user_id == ADMIN_ID
        ),
    )


# ============================================================
# PHOTO ROUTER
# ============================================================

async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(update.effective_user)

    if is_banned(user_id):
        await send_ui_message(update, context, 
            "🚫 <b>Քո հաշիվը արգելափակված է։</b>",
            parse_mode="HTML",
        )
        return

    if context.user_data.get("step") == "photo":
        await handle_profile_photo(update, context)
        return

    # If a photo is sent during chat, don't treat it as a chat message.
    if context.user_data.get("chat_match_id"):
        await send_ui_message(update, context, 
            "💬 Չատում այս տարբերակում ուղարկիր տեքստային հաղորդագրություն։"
        )
        return


# ============================================================
# COMMANDS
# ============================================================

async def help_command(update, context):
    ensure_user(update.effective_user)
    touch(update.effective_user.id)

    await send_ui_message(update, context, 
        "ℹ️ <b>Together</b>\n\n"
        "👤 Ստեղծիր և խմբագրիր պրոֆիլդ\n"
        "🔎 Գտիր մարդկանց\n"
        "❤️ Ստեղծիր Match\n"
        "💬 Շփվիր Match-երիդ հետ\n"
        "🚫 Բլոկավորիր օգտատերերին\n"
        "🚨 Ուղարկիր հաղորդումներ\n\n"
        "Սկսելու համար՝ /start",
        parse_mode="HTML",
        reply_markup=reply_main_keyboard(
            update.effective_user.id == ADMIN_ID
        ),
    )


async def cancel_command(update, context):
    context.user_data.clear()
    touch(update.effective_user.id)

    await send_ui_message(update, context, 
        "❌ Գործողությունը չեղարկվեց։",
        reply_markup=reply_main_keyboard(
            update.effective_user.id == ADMIN_ID
        ),
    )


# ============================================================
# APPLICATION
# ============================================================

async def post_init(application):
    init_db()

    # Temporary bot UI registry used by the 3-minute cleanup.
    application.togethr_ui_messages = {}

    if application.job_queue:
        application.job_queue.run_repeating(
            inactivity_cleanup,
            interval=10,
            first=10,
            name="togethr-inactivity-cleanup",
        )

    logger.info("Together bot initialized.")


def build_application():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set. "
            "Set the BOT_TOKEN environment variable."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_router)
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_router,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    return application


def main():
    init_db()

    application = build_application()

    logger.info("Starting Together bot...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

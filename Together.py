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
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_FILE = "togethr.db"
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# ============================================================
# TIME
# ============================================================
def now_utc():
    return datetime.now(timezone.utc)
def now_str():
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
def iso_now():
    return now_utc().isoformat()
# ============================================================
# DATABASE
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn
def init_database():
    conn = get_db()
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
            text TEXT NOT NULL,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter INTEGER NOT NULL,
            reported INTEGER NOT NULL,
            reason TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            details TEXT,
            created_at TEXT
        )
    """)
    # Migration for old databases
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_active TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
# ============================================================
# ADMIN LOGGING
# ============================================================
def db_log(user_id, action, details=""):
    try:
        conn = get_db()
        conn.execute(
            """
            INSERT INTO activity_logs
            (user_id, action, details, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, action, details, iso_now()),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Could not save activity log")
def user_label(user):
    if not user:
        return "Unknown user"
    username = user["username"] or ""
    username_text = f"@{username}" if username else "—"
    return (
        f"{user['name'] or '—'} "
        f"({username_text}) "
        f"[ID: {user['id']}]"
    )
async def admin_notify(context, text):
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Could not notify admin")
async def log_admin(context, user_id, action, details):
    db_log(user_id, action, details)
    user = get_user(user_id)
    if user:
        actor = user_label(user)
    else:
        actor = f"ID: {user_id}"
    text = (
        "📡 <b>TOGETHR ACTIVITY</b>\n\n"
        f"👤 <b>User:</b> {html.escape(actor)}\n"
        f"⚡ <b>Action:</b> {html.escape(action)}\n"
        f"📝 <b>Details:</b>\n{html.escape(details)}\n\n"
        f"🕐 {now_str()}"
    )
    await admin_notify(context, text)
# ============================================================
# USERS
# ============================================================
def create_user(user_id, username=None, name=None):
    conn = get_db()
    cur = conn.cursor()
    existing = cur.execute(
        "SELECT id FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if existing:
        cur.execute(
            """
            UPDATE users
            SET username = ?,
                last_active = ?
            WHERE id = ?
            """,
            (username, iso_now(), user_id),
        )
    else:
        cur.execute(
            """
            INSERT INTO users
            (id, username, name, created_at, last_active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                name,
                iso_now(),
                iso_now(),
            ),
        )
    conn.commit()
    conn.close()
def get_user(user_id):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return user
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
    fields = {
        key: value
        for key, value in fields.items()
        if key in allowed
    }
    if not fields:
        return
    assignments = ", ".join(
        f"{key} = ?" for key in fields
    )
    values = list(fields.values())
    values.append(user_id)
    conn = get_db()
    conn.execute(
        f"UPDATE users SET {assignments} WHERE id = ?",
        values,
    )
    conn.commit()
    conn.close()
def update_last_active(user_id):
    update_user(user_id, last_active=iso_now())
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
    if not all(required):
        return False
    if user["gender"] not in ("Տղամարդ", "Կին"):
        return False
    if user["looking_for"] not in ("Տղամարդ", "Կին"):
        return False
    return True
# ============================================================
# PROFILE DISPLAY
# ============================================================
def profile_text(user):
    name = html.escape(str(user["name"] or "—"))
    age = html.escape(str(user["age"] or "—"))
    city = html.escape(str(user["city"] or "—"))
    gender = html.escape(str(user["gender"] or "—"))
    looking = html.escape(str(user["looking_for"] or "—"))
    about = html.escape(str(user["about"] or "—"))
    return (
        f"👤 <b>{name}, {age}</b>\n\n"
        f"📍 {city}\n"
        f"⚧️ {gender}\n"
        f"🔎 Փնտրում է՝ {looking}\n\n"
        f"💬 <b>Իմ մասին</b>\n"
        f"{about}"
    )
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                "👤 Իմ պրոֆիլը",
                "🔎 Գտնել մարդկանց",
            ],
            [
                "❤️ Իմ Match-երը",
                "✏️ Խմբագրել պրոֆիլը",
            ],
        ],
        resize_keyboard=True,
    )
def profile_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✏️ Խմբագրել",
                callback_data="edit_profile",
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
def gender_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👨 Տղամարդ",
                callback_data="gender:Տղամարդ",
            ),
            InlineKeyboardButton(
                "👩 Կին",
                callback_data="gender:Կին",
            ),
        ],
        [
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            ),
        ],
    ])
def looking_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👨 Տղամարդ",
                callback_data="looking:Տղամարդ",
            ),
            InlineKeyboardButton(
                "👩 Կին",
                callback_data="looking:Կին",
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅️ Նախորդ",
                callback_data="profile_back",
            ),
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            ),
        ],
    ])
def profile_back_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ Նախորդ",
                callback_data="profile_back",
            ),
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            ),
        ],
    ])
# ============================================================
# SAFE MESSAGE EDIT
# ============================================================
async def safe_edit_to_text(query, text, reply_markup=None):
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        return
    except Exception:
        pass
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.message.chat.send_message(
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
async def show_home(query, context):
    await safe_edit_to_text(
        query,
        "🏠 <b>Գլխավոր մենյու</b>\n\n"
        "Ընտրիր գործողությունը։",
    )
    try:
        await query.message.chat.send_message(
            "Օգտագործիր ներքևի մենյուն 👇",
            reply_markup=main_keyboard(),
        )
    except Exception:
        pass
# ============================================================
# PROFILE CREATION / EDITING
# ============================================================
async def show_profile_step(query, context, step):
    context.user_data["step"] = step
    prompts = {
        "name": (
            "👤 <b>Քայլ 1/7</b>\n\n"
            "Գրիր քո անունը։"
        ),
        "age": (
            "🎂 <b>Քայլ 2/7</b>\n\n"
            "Գրիր քո տարիքը։\n"
            "Տարիքը պետք է լինի 18-99։"
        ),
        "city": (
            "📍 <b>Քայլ 3/7</b>\n\n"
            "Գրիր քո քաղաքը։"
        ),
        "gender": (
            "⚧️ <b>Քայլ 4/7</b>\n\n"
            "Ընտրիր քո սեռը։"
        ),
        "looking": (
            "🔎 <b>Քայլ 5/7</b>\n\n"
            "Ո՞ւմ ես փնտրում։"
        ),
        "about": (
            "💬 <b>Քայլ 6/7</b>\n\n"
            "Մի փոքր պատմիր քո մասին։"
        ),
        "photo": (
            "📸 <b>Քայլ 7/7</b>\n\n"
            "Ուղարկիր քո լուսանկարը։\n\n"
            "⚠️ Լուսանկարը պարտադիր է։"
        ),
    }
    keyboards = {
        "name": profile_back_keyboard(),
        "age": profile_back_keyboard(),
        "city": profile_back_keyboard(),
        "gender": gender_keyboard(),
        "looking": looking_keyboard(),
        "about": profile_back_keyboard(),
        "photo": profile_back_keyboard(),
    }
    await safe_edit_to_text(
        query,
        prompts[step],
        keyboards[step],
    )
async def start_profile(update, context, editing=False):
    user = update.effective_user
    create_user(
        user.id,
        user.username,
        user.first_name,
    )
    context.user_data["editing"] = editing
    context.user_data["step"] = "name"
    update_last_active(user.id)
    if editing:
        text = (
            "✏️ <b>Խմբագրել պրոֆիլը</b>\n\n"
            "👤 Գրիր քո անունը։"
        )
    else:
        text = (
            "❤️ <b>Բարի գալուստ Togethr</b>\n\n"
            "Սկսենք քո պրոֆիլի ստեղծումը։\n\n"
            "👤 Գրիր քո անունը։"
        )
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=profile_back_keyboard(),
    )
    await log_admin(
        context,
        user.id,
        "PROFILE_STARTED",
        f"Editing: {editing}",
    )
# ============================================================
# PHOTO HANDLER
# ============================================================
async def profile_photo_handler(update, context):
    user = update.effective_user
    if is_banned(user.id):
        return
    step = context.user_data.get("step")
    if step != "photo":
        return
    if not update.message.photo:
        return
    photo = update.message.photo[-1]
    update_user(
        user.id,
        photo_file_id=photo.file_id,
    )
    update_last_active(user.id)
    context.user_data["step"] = None
    context.user_data["editing"] = False
    saved_user = get_user(user.id)
    await update.message.reply_text(
        "✅ <b>Պրոֆիլը պատրաստ է։</b>\n\n"
        + profile_text(saved_user),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )
    await log_admin(
        context,
        user.id,
        "PROFILE_PHOTO_UPDATED",
        "Օգտատերը ավելացրեց/փոխեց պրոֆիլի լուսանկարը։",
    )
    await log_admin(
        context,
        user.id,
        "PROFILE_COMPLETED",
        "Օգտատերը ավարտեց պրոֆիլի ստեղծումը/խմբագրումը։",
    )
# ============================================================
# MY PROFILE
# ============================================================
async def show_my_profile_message(update, context):
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text(
            "Սկզբում ստեղծիր պրոֆիլը։"
        )
        return
    if not profile_is_complete(user):
        await update.message.reply_text(
            "⚠️ Քո պրոֆիլը դեռ ամբողջական չէ։\n"
            "Սկսենք լրացնել այն։"
        )
        await start_profile(update, context, editing=True)
        return
    update_last_active(user["id"])
    if user["photo_file_id"]:
        await update.message.reply_photo(
            photo=user["photo_file_id"],
            caption=profile_text(user),
            parse_mode="HTML",
            reply_markup=profile_keyboard(),
        )
    else:
        await update.message.reply_text(
            profile_text(user),
            parse_mode="HTML",
            reply_markup=profile_keyboard(),
        )
async def show_my_profile_callback(query, context):
    user = get_user(query.from_user.id)
    if not user:
        await query.answer("Սկզբում ստեղծիր պրոֆիլը։", show_alert=True)
        return
    if not profile_is_complete(user):
        await query.answer(
            "Պրոֆիլը ամբողջական չէ։",
            show_alert=True,
        )
        await start_profile_from_callback(query, context)
        return
    update_last_active(user["id"])
    try:
        await query.message.delete()
    except Exception:
        pass
    if user["photo_file_id"]:
        await query.message.chat.send_photo(
            photo=user["photo_file_id"],
            caption=profile_text(user),
            parse_mode="HTML",
            reply_markup=profile_keyboard(),
        )
    else:
        await query.message.chat.send_message(
            profile_text(user),
            parse_mode="HTML",
            reply_markup=profile_keyboard(),
        )
async def start_profile_from_callback(query, context):
    context.user_data["editing"] = True
    context.user_data["step"] = "name"
    await safe_edit_to_text(
        query,
        "✏️ <b>Խմբագրել պրոֆիլը</b>\n\n"
        "👤 Գրիր քո անունը։",
        profile_back_keyboard(),
    )
# ============================================================
# DISCOVERY
# ============================================================
def compatible(viewer, candidate):
    if not viewer or not candidate:
        return False
    if viewer["gender"] not in ("Տղամարդ", "Կին"):
        return False
    if candidate["gender"] not in ("Տղամարդ", "Կին"):
        return False
    if viewer["looking_for"] != candidate["gender"]:
        return False
    if candidate["looking_for"] != viewer["gender"]:
        return False
    return True
def get_next_profile(user_id):
    viewer = get_user(user_id)
    if not viewer:
        return None
    active_since = (
        now_utc() - timedelta(days=7)
    ).isoformat()
    conn = get_db()
    candidates = conn.execute(
        """
        SELECT *
        FROM users
        WHERE id != ?
          AND banned = 0
          AND name IS NOT NULL
          AND age IS NOT NULL
          AND city IS NOT NULL
          AND gender IS NOT NULL
          AND looking_for IS NOT NULL
          AND about IS NOT NULL
          AND photo_file_id IS NOT NULL
          AND gender IN ('Տղամարդ', 'Կին')
          AND looking_for IN ('Տղամարդ', 'Կին')
          AND last_active >= ?
        ORDER BY RANDOM()
        """,
        (user_id, active_since),
    ).fetchall()
    conn.close()
    for candidate in candidates:
        if compatible(viewer, candidate):
            if not has_swiped(user_id, candidate["id"]):
                return candidate
    return None
def has_swiped(from_user, to_user):
    conn = get_db()
    row = conn.execute(
        """
        SELECT id
        FROM swipes
        WHERE from_user = ?
          AND to_user = ?
        """,
        (from_user, to_user),
    ).fetchone()
    conn.close()
    return row is not None
def swipe_keyboard(target_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Բաց թողնել",
                callback_data=f"swipe:pass:{target_id}",
            ),
            InlineKeyboardButton(
                "❤️ Հավանել",
                callback_data=f"swipe:like:{target_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⭐ Super Like",
                callback_data=f"swipe:super:{target_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚨 Report",
                callback_data=f"report:{target_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🏠 Գլխավոր",
                callback_data="home",
            ),
        ],
    ])
async def discover(query, context):
    user_id = query.from_user.id
    if is_banned(user_id):
        await query.answer(
            "Քո հաշիվը արգելափակված է։",
            show_alert=True,
        )
        return
    viewer = get_user(user_id)
    if not profile_is_complete(viewer):
        await query.answer(
            "Սկզբում լրացրու ամբողջական պրոֆիլը։",
            show_alert=True,
        )
        await start_profile_from_callback(query, context)
        return
    update_last_active(user_id)
    candidate = get_next_profile(user_id)
    if not candidate:
        await safe_edit_to_text(
            query,
            "🔎 <b>Այս պահին նոր պրոֆիլ չկա։</b>\n\n"
            "Փորձիր ավելի ուշ։",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🏠 Գլխավոր",
                        callback_data="home",
                    )
                ]
            ]),
        )
        return
    text = profile_text(candidate)
    await log_admin(
        context,
        user_id,
        "PROFILE_VIEWED",
        (
            f"Օգտատերը դիտեց պրոֆիլը.\n"
            f"Target: {user_label(candidate)}"
        ),
    )
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.message.chat.send_photo(
        photo=candidate["photo_file_id"],
        caption=text,
        parse_mode="HTML",
        reply_markup=swipe_keyboard(candidate["id"]),
    )
# ============================================================
# SWIPES
# ============================================================
def save_swipe(from_user, to_user, action):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO swipes
        (from_user, to_user, action, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(from_user, to_user)
        DO UPDATE SET
            action = excluded.action,
            created_at = excluded.created_at
        """,
        (
            from_user,
            to_user,
            action,
            iso_now(),
        ),
    )
    conn.commit()
    conn.close()
def get_swipe(from_user, to_user):
    conn = get_db()
    row = conn.execute(
        """
        SELECT *
        FROM swipes
        WHERE from_user = ?
          AND to_user = ?
        """,
        (from_user, to_user),
    ).fetchone()
    conn.close()
    return row
def create_match(user1, user2):
    a = min(user1, user2)
    b = max(user1, user2)
    conn = get_db()
    existing = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE user1 = ?
          AND user2 = ?
        """,
        (a, b),
    ).fetchone()
    if existing:
        conn.close()
        return existing["id"]
    cur = conn.execute(
        """
        INSERT INTO matches
        (user1, user2, created_at)
        VALUES (?, ?, ?)
        """,
        (a, b, iso_now()),
    )
    match_id = cur.lastrowid
    conn.commit()
    conn.close()
    return match_id
def get_match(match_id):
    conn = get_db()
    row = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE id = ?
        """,
        (match_id,),
    ).fetchone()
    conn.close()
    return row
def get_other_user(match, user_id):
    if match["user1"] == user_id:
        return match["user2"]
    return match["user1"]
async def handle_swipe(query, context, action, target_id):
    actor_id = query.from_user.id
    await query.answer()
    actor = get_user(actor_id)
    target = get_user(target_id)
    if not actor or not target:
        await safe_edit_to_text(
            query,
            "⚠️ Պրոֆիլը այլևս հասանելի չէ։",
        )
        return
    if target["banned"]:
        await discover(query, context)
        return
    update_last_active(actor_id)
    save_swipe(actor_id, target_id, action)
    action_names = {
        "like": "❤️ Like",
        "super": "⭐ Super Like",
        "pass": "❌ Pass",
    }
    await log_admin(
        context,
        actor_id,
        "SWIPE",
        (
            f"Action: {action_names.get(action, action)}\n"
            f"Target: {user_label(target)}"
        ),
    )
    # PASS
    if action == "pass":
        await discover(query, context)
        return
    reciprocal = get_swipe(
        target_id,
        actor_id,
    )
    reciprocal_positive = (
        reciprocal
        and reciprocal["action"] in ("like", "super")
    )
    # MATCH
    if reciprocal_positive:
        match_id = create_match(
            actor_id,
            target_id,
        )
        await log_admin(
            context,
            actor_id,
            "MATCH_CREATED",
            (
                f"Match ID: {match_id}\n"
                f"User 1: {user_label(actor)}\n"
                f"User 2: {user_label(target)}"
            ),
        )
        match_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💬 Գրել",
                    callback_data=f"chat:{match_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔎 Շարունակել",
                    callback_data="discover",
                ),
            ],
        ])
        try:
            await context.bot.send_message(
                chat_id=actor_id,
                text=(
                    "🎉 <b>Նոր Match!</b>\n\n"
                    f"Դու և <b>{html.escape(target['name'])}</b> "
                    "հավանել եք միմյանց ❤️\n\n"
                    "Կարող եք սկսել զրուցել։"
                ),
                parse_mode="HTML",
                reply_markup=match_keyboard,
            )
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "🎉 <b>Նոր Match!</b>\n\n"
                    f"Դու և <b>{html.escape(actor['name'])}</b> "
                    "հավանել եք միմյանց ❤️\n\n"
                    "Կարող եք սկսել զրուցել։"
                ),
                parse_mode="HTML",
                reply_markup=match_keyboard,
            )
        except Exception:
            pass
        await safe_edit_to_text(
            query,
            "🎉 <b>Match!</b>\n\n"
            f"Դու և {html.escape(target['name'])} "
            "հավանել եք միմյանց ❤️",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💬 Գրել",
                        callback_data=f"chat:{match_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔎 Շարունակել",
                        callback_data="discover",
                    )
                ],
            ]),
        )
        return
    # NORMAL LIKE
    if action == "like":
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "❤️ <b>Ինչ-որ մեկը հավանել է քո պրոֆիլը!</b>\n\n"
                    "Գնա «Գտնել մարդկանց» բաժին՝ շարունակելու համար։"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔎 Գտնել մարդկանց",
                            callback_data="discover",
                        )
                    ]
                ]),
            )
        except Exception:
            pass
        await log_admin(
            context,
            actor_id,
            "LIKE_NOTIFICATION",
            (
                f"Like notification sent to: "
                f"{user_label(target)}"
            ),
        )
    # SUPER LIKE
    elif action == "super":
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "⭐ <b>Դու ստացել ես Super Like!</b>\n\n"
                    "Ինչ-որ մեկը քեզ շատ է հավանել ❤️"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔎 Գտնել մարդկանց",
                            callback_data="discover",
                        )
                    ]
                ]),
            )
        except Exception:
            pass
        await log_admin(
            context,
            actor_id,
            "SUPER_LIKE_NOTIFICATION",
            (
                f"Super Like notification sent to: "
                f"{user_label(target)}"
            ),
        )
    await discover(query, context)
# ============================================================
# MATCHES
# ============================================================
def get_user_matches(user_id):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE user1 = ?
           OR user2 = ?
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
            "❤️ <b>Դեռ Match չունես։</b>\n\n"
            "Գնա «Գտնել մարդկանց» բաժին։",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return
    keyboard = []
    for match in matches:
        other_id = get_other_user(match, user_id)
        other = get_user(other_id)
        if other:
            keyboard.append([
                InlineKeyboardButton(
                    f"💬 {other['name']}",
                    callback_data=f"chat:{match['id']}",
                )
            ])
    keyboard.append([
        InlineKeyboardButton(
            "🏠 Գլխավոր",
            callback_data="home",
        )
    ])
    await update.message.reply_text(
        "❤️ <b>Իմ Match-երը</b>\n\n"
        "Ընտրիր մարդու հետ զրույցը։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
# ============================================================
# CHAT
# ============================================================
async def open_chat(query, context, match_id):
    user_id = query.from_user.id
    match = get_match(match_id)
    if not match:
        await query.answer(
            "Match-ը չի գտնվել։",
            show_alert=True,
        )
        return
    if user_id not in (
        match["user1"],
        match["user2"],
    ):
        await query.answer(
            "Այս Match-ը քոնը չէ։",
            show_alert=True,
        )
        return
    other_id = get_other_user(
        match,
        user_id,
    )
    other = get_user(other_id)
    if not other:
        return
    context.user_data["chat_match_id"] = match_id
    await query.answer()
    await safe_edit_to_text(
        query,
        (
            "💬 <b>Չատ</b>\n\n"
            f"Զրուցում ես՝ <b>{html.escape(other['name'])}</b>\n\n"
            "Գրիր հաղորդագրություն 👇\n\n"
            "Չատից դուրս գալու համար սեղմիր «🏠 Գլխավոր»։"
        ),
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🏠 Գլխավոր",
                    callback_data="home",
                )
            ]
        ]),
    )
async def chat_message(update, context):
    user_id = update.effective_user.id
    match_id = context.user_data.get("chat_match_id")
    if not match_id:
        return False
    if not update.message.text:
        return True
    match = get_match(match_id)
    if not match:
        context.user_data.pop(
            "chat_match_id",
            None,
        )
        return True
    if user_id not in (
        match["user1"],
        match["user2"],
    ):
        context.user_data.pop(
            "chat_match_id",
            None,
        )
        return True
    text = update.message.text.strip()
    if not text:
        return True
    other_id = get_other_user(
        match,
        user_id,
    )
    other = get_user(other_id)
    sender = get_user(user_id)
    if not other:
        return True
    conn = get_db()
    conn.execute(
        """
        INSERT INTO messages
        (match_id, sender_id, text, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            match_id,
            user_id,
            text,
            iso_now(),
        ),
    )
    conn.commit()
    conn.close()
    update_last_active(user_id)
    # Forward to recipient
    try:
        await context.bot.send_message(
            chat_id=other_id,
            text=(
                f"💬 <b>{html.escape(sender['name'])}</b>\n\n"
                f"{html.escape(text)}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💬 Պատասխանել",
                        callback_data=f"chat:{match_id}",
                    )
                ]
            ]),
        )
    except Exception:
        logger.exception("Could not forward chat message")
    # ADMIN GETS EVERY CHAT MESSAGE
    await log_admin(
        context,
        user_id,
        "CHAT_MESSAGE",
        (
            f"Match ID: {match_id}\n"
            f"Sender: {user_label(sender)}\n"
            f"Recipient: {user_label(other)}\n\n"
            f"Message:\n{text}"
        ),
    )
    await update.message.reply_text(
        "✅ Ուղարկվեց",
    )
    return True
# ============================================================
# REPORTS
# ============================================================
def create_report(reporter, reported, reason):
    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO reports
        (reporter, reported, reason, status, created_at)
        VALUES (?, ?, ?, 'open', ?)
        """,
        (
            reporter,
            reported,
            reason,
            iso_now(),
        ),
    )
    report_id = cur.lastrowid
    conn.commit()
    conn.close()
    return report_id
async def show_report_options(query, context, target_id):
    await query.answer()
    context.user_data["report_target"] = target_id
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚫 Անպատշաճ բովանդակություն",
                callback_data="report_reason:inappropriate",
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 Fake / Spam",
                callback_data="report_reason:fake",
            )
        ],
        [
            InlineKeyboardButton(
                "😡 Վիրավորանք / վատ վարք",
                callback_data="report_reason:abuse",
            )
        ],
        [
            InlineKeyboardButton(
                "⚠️ Այլ",
                callback_data="report_reason:other",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Չեղարկել",
                callback_data="discover",
            )
        ],
    ])
    await safe_edit_to_text(
        query,
        "🚨 <b>Ինչո՞ւ ես Report անում այս պրոֆիլը:</b>",
        keyboard,
    )
async def handle_report_reason(query, context, reason):
    reporter_id = query.from_user.id
    reported_id = context.user_data.get(
        "report_target"
    )
    if not reported_id:
        await query.answer(
            "Report-ի տվյալները չեն գտնվել։",
            show_alert=True,
        )
        return
    reasons = {
        "inappropriate": "Անպատշաճ բովանդակություն",
        "fake": "Fake / Spam",
        "abuse": "Վիրավորանք / վատ վարք",
        "other": "Այլ",
    }
    reason_text = reasons.get(
        reason,
        "Այլ",
    )
    report_id = create_report(
        reporter_id,
        reported_id,
        reason_text,
    )
    reporter = get_user(reporter_id)
    reported = get_user(reported_id)
    await query.answer(
        "Report-ը ուղարկվեց Admin-ին։",
        show_alert=True,
    )
    await safe_edit_to_text(
        query,
        "🚨 <b>Report-ը ուղարկվեց։</b>\n\n"
        "Շնորհակալություն տեղեկացնելու համար։",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔎 Շարունակել",
                    callback_data="discover",
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
    await log_admin(
        context,
        reporter_id,
        "REPORT_CREATED",
        (
            f"Report ID: {report_id}\n"
            f"Reporter: {user_label(reporter)}\n"
            f"Reported: {user_label(reported)}\n"
            f"Reason: {reason_text}"
        ),
    )
# ============================================================
# PROFILE TEXT ROUTER
# ============================================================
async def text_router(update, context):
    user = update.effective_user
    if not update.message or not update.message.text:
        return
    if is_banned(user.id):
        await update.message.reply_text(
            "🚫 Քո հաշիվը արգելափակված է։"
        )
        return
    create_user(
        user.id,
        user.username,
        user.first_name,
    )
    update_last_active(user.id)
    # If currently chatting
    if context.user_data.get("chat_match_id"):
        handled = await chat_message(
            update,
            context,
        )
        if handled:
            return
    text = update.message.text.strip()
    step = context.user_data.get("step")
    # MAIN MENU
    if text == "👤 Իմ պրոֆիլը":
        await show_my_profile_message(
            update,
            context,
        )
        return
    if text == "🔎 Գտնել մարդկանց":
        user_obj = get_user(user.id)
        if not profile_is_complete(user_obj):
            await update.message.reply_text(
                "⚠️ Սկզբում լրացրու քո ամբողջական պրոֆիլը։"
            )
            await start_profile(
                update,
                context,
                editing=True,
            )
            return
        # Fake query-like discovery is handled separately
        candidate = get_next_profile(user.id)
        if not candidate:
            await update.message.reply_text(
                "🔎 Այս պահին նոր պրոֆիլ չկա։",
                reply_markup=main_keyboard(),
            )
            return
        await log_admin(
            context,
            user.id,
            "PROFILE_VIEWED",
            f"Target: {user_label(candidate)}",
        )
        await update.message.reply_photo(
            photo=candidate["photo_file_id"],
            caption=profile_text(candidate),
            parse_mode="HTML",
            reply_markup=swipe_keyboard(candidate["id"]),
        )
        return
    if text == "❤️ Իմ Match-երը":
        await show_matches_message(
            update,
            context,
        )
        return
    if text == "✏️ Խմբագրել պրոֆիլը":
        await start_profile(
            update,
            context,
            editing=True,
        )
        return
    # PROFILE STEPS
    if step == "name":
        if len(text) < 2:
            await update.message.reply_text(
                "❌ Անունը շատ կարճ է։ Փորձիր կրկին։"
            )
            return
        update_user(
            user.id,
            name=text,
        )
        await log_admin(
            context,
            user.id,
            "PROFILE_NAME_UPDATED",
            f"Name: {text}",
        )
        context.user_data["step"] = "age"
        await update.message.reply_text(
            "🎂 <b>Քայլ 2/7</b>\n\n"
            "Գրիր քո տարիքը։\n"
            "Տարիքը պետք է լինի 18-99։",
            parse_mode="HTML",
            reply_markup=profile_back_keyboard(),
        )
        return
    if step == "age":
        try:
            age = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Գրիր միայն թիվ։ Օրինակ՝ 25"
            )
            return
        if age < 18 or age > 99:
            await update.message.reply_text(
                "❌ Տարիքը պետք է լինի 18-99։"
            )
            return
        update_user(
            user.id,
            age=age,
        )
        await log_admin(
            context,
            user.id,
            "PROFILE_AGE_UPDATED",
            f"Age: {age}",
        )
        context.user_data["step"] = "city"
        await update.message.reply_text(
            "📍 <b>Քայլ 3/7</b>\n\n"
            "Գրիր քո քաղաքը։",
            parse_mode="HTML",
            reply_markup=profile_back_keyboard(),
        )
        return
    if step == "city":
        if len(text) < 2:
            await update.message.reply_text(
                "❌ Քաղաքը ճիշտ լրացրու։"
            )
            return
        update_user(
            user.id,
            city=text,
        )
        await log_admin(
            context,
            user.id,
            "PROFILE_CITY_UPDATED",
            f"City: {text}",
        )
        context.user_data["step"] = "gender"
        await update.message.reply_text(
            "⚧️ <b>Քայլ 4/7</b>\n\n"
            "Ընտրիր քո սեռը։",
            parse_mode="HTML",
            reply_markup=gender_keyboard(),
        )
        return
    if step == "about":
        if len(text) < 2:
            await update.message.reply_text(
                "❌ Գրիր գոնե մի փոքր քո մասին։"
            )
            return
        update_user(
            user.id,
            about=text,
        )
        await log_admin(
            context,
            user.id,
            "PROFILE_ABOUT_UPDATED",
            f"About: {text}",
        )
        context.user_data["step"] = "photo"
        await update.message.reply_text(
            "📸 <b>Քայլ 7/7</b>\n\n"
            "Ուղարկիր քո լուսանկարը։\n\n"
            "⚠️ Լուսանկարը պարտադիր է։",
            parse_mode="HTML",
            reply_markup=profile_back_keyboard(),
        )
        return
    if step == "photo":
        await update.message.reply_text(
            "📸 Այս քայլը պարտադիր է։\n\n"
            "Ուղարկիր լուսանկար՝ որպես Photo։"
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
    data = query.data
    user_id = query.from_user.id
    if is_banned(user_id):
        await query.answer(
            "Քո հաշիվը արգելափակված է։",
            show_alert=True,
        )
        return
    update_last_active(user_id)
    # HOME
    if data == "home":
        await query.answer()
        context.user_data.pop(
            "chat_match_id",
            None,
        )
        await show_home(
            query,
            context,
        )
        return
    # EDIT PROFILE
    if data == "edit_profile":
        await query.answer()
        context.user_data["editing"] = True
        context.user_data["step"] = "name"
        await safe_edit_to_text(
            query,
            "✏️ <b>Խմբագրել պրոֆիլը</b>\n\n"
            "👤 Գրիր քո անունը։",
            profile_back_keyboard(),
        )
        return
    # MY PROFILE
    if data == "my_profile":
        await query.answer()
        await show_my_profile_callback(
            query,
            context,
        )
        return
    # DISCOVER
    if data == "discover":
        await discover(
            query,
            context,
        )
        return
    # GENDER
    if data.startswith("gender:"):
        await query.answer()
        gender = data.split(":", 1)[1]
        if gender not in ("Տղամարդ", "Կին"):
            return
        update_user(
            user_id,
            gender=gender,
        )
        await log_admin(
            context,
            user_id,
            "PROFILE_GENDER_UPDATED",
            f"Gender: {gender}",
        )
        context.user_data["step"] = "looking"
        await safe_edit_to_text(
            query,
            "🔎 <b>Քայլ 5/7</b>\n\n"
            "Ո՞ւմ ես փնտրում։",
            looking_keyboard(),
        )
        return
    # LOOKING FOR
    if data.startswith("looking:"):
        await query.answer()
        looking = data.split(":", 1)[1]
        if looking not in ("Տղամարդ", "Կին"):
            return
        update_user(
            user_id,
            looking_for=looking,
        )
        await log_admin(
            context,
            user_id,
            "PROFILE_LOOKING_FOR_UPDATED",
            f"Looking for: {looking}",
        )
        context.user_data["step"] = "about"
        await safe_edit_to_text(
            query,
            "💬 <b>Քայլ 6/7</b>\n\n"
            "Մի փոքր պատմիր քո մասին։",
            profile_back_keyboard(),
        )
        return
    # PROFILE BACK
    if data == "profile_back":
        await query.answer()
        step = context.user_data.get("step")
        previous = {
            "age": "name",
            "city": "age",
            "gender": "city",
            "looking": "gender",
            "about": "looking",
            "photo": "about",
        }
        if step == "name":
            context.user_data["step"] = None
            context.user_data["editing"] = False
            await safe_edit_to_text(
                query,
                "🏠 <b>Գլխավոր մենյու</b>",
            )
            try:
                await query.message.chat.send_message(
                    "Ընտրիր գործողություն 👇",
                    reply_markup=main_keyboard(),
                )
            except Exception:
                pass
            return
        prev = previous.get(step)
        if not prev:
            return
        context.user_data["step"] = prev
        # Display previous prompt
        if prev == "name":
            await safe_edit_to_text(
                query,
                "👤 <b>Քայլ 1/7</b>\n\n"
                "Գրիր քո անունը։",
                profile_back_keyboard(),
            )
        elif prev == "age":
            await safe_edit_to_text(
                query,
                "🎂 <b>Քայլ 2/7</b>\n\n"
                "Գրիր քո տարիքը։",
                profile_back_keyboard(),
            )
        elif prev == "city":
            await safe_edit_to_text(
                query,
                "📍 <b>Քայլ 3/7</b>\n\n"
                "Գրիր քո քաղաքը։",
                profile_back_keyboard(),
            )
        elif prev == "gender":
            await safe_edit_to_text(
                query,
                "⚧️ <b>Քայլ 4/7</b>\n\n"
                "Ընտրիր քո սեռը։",
                gender_keyboard(),
            )
        elif prev == "looking":
            await safe_edit_to_text(
                query,
                "🔎 <b>Քայլ 5/7</b>\n\n"
                "Ո՞ւմ ես փնտրում։",
                looking_keyboard(),
            )
        elif prev == "about":
            await safe_edit_to_text(
                query,
                "💬 <b>Քայլ 6/7</b>\n\n"
                "Մի փոքր պատմիր քո մասին։",
                profile_back_keyboard(),
            )
        return
    # SWIPE
    if data.startswith("swipe:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Սխալ գործողություն։")
            return
        action = parts[1]
        try:
            target_id = int(parts[2])
        except ValueError:
            await query.answer("Սխալ պրոֆիլ։")
            return
        if action not in ("like", "pass", "super"):
            await query.answer("Սխալ գործողություն։")
            return
        await handle_swipe(
            query,
            context,
            action,
            target_id,
        )
        return
    # REPORT
    if data.startswith("report:"):
        try:
            target_id = int(
                data.split(":", 1)[1]
            )
        except ValueError:
            await query.answer(
                "Սխալ պրոֆիլ։",
                show_alert=True,
            )
            return
        await show_report_options(
            query,
            context,
            target_id,
        )
        return
    # REPORT REASON
    if data.startswith("report_reason:"):
        reason = data.split(
            ":",
            1,
        )[1]
        await handle_report_reason(
            query,
            context,
            reason,
        )
        return
    # CHAT
    if data.startswith("chat:"):
        try:
            match_id = int(
                data.split(":", 1)[1]
            )
        except ValueError:
            await query.answer(
                "Սխալ Match։",
                show_alert=True,
            )
            return
        await open_chat(
            query,
            context,
            match_id,
        )
        return
    await query.answer(
        "Անհայտ գործողություն։"
    )
# ============================================================
# COMMANDS
# ============================================================
async def start_command(update, context):
    user = update.effective_user
    create_user(
        user.id,
        user.username,
        user.first_name,
    )
    update_last_active(user.id)
    user_obj = get_user(user.id)
    if is_banned(user.id):
        await update.message.reply_text(
            "🚫 Քո հաշիվը արգելափակված է։"
        )
        return
    if profile_is_complete(user_obj):
        await update.message.reply_text(
            "❤️ <b>Բարի վերադարձ Togethr</b>!",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    else:
        await start_profile(
            update,
            context,
            editing=False,
        )
        await log_admin(
            context,
            user.id,
            "USER_STARTED_BOT",
            "Օգտատերը սեղմեց /start։",
        )
async def help_command(update, context):
    await update.message.reply_text(
        "ℹ️ <b>Togethr Help</b>\n\n"
        "👤 Իմ պրոֆիլը — տես քո պրոֆիլը\n"
        "🔎 Գտնել մարդկանց — գտիր մարդկանց\n"
        "❤️ Իմ Match-երը — քո Match-երը\n"
        "✏️ Խմբագրել պրոֆիլը — փոխիր տվյալները\n\n"
        "❤️ Like\n"
        "⭐ Super Like\n"
        "❌ Pass\n"
        "🚨 Report",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )
# ============================================================
# ADMIN COMMANDS
# ============================================================
def admin_only(user_id):
    return ADMIN_ID and user_id == ADMIN_ID
async def admin_command(update, context):
    if not admin_only(update.effective_user.id):
        return
    conn = get_db()
    users = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]
    active_since = (
        now_utc() - timedelta(days=7)
    ).isoformat()
    active = conn.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE banned = 0
          AND last_active >= ?
        """,
        (active_since,),
    ).fetchone()[0]
    banned = conn.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE banned = 1
        """
    ).fetchone()[0]
    likes = conn.execute(
        """
        SELECT COUNT(*)
        FROM swipes
        WHERE action = 'like'
        """
    ).fetchone()[0]
    super_likes = conn.execute(
        """
        SELECT COUNT(*)
        FROM swipes
        WHERE action = 'super'
        """
    ).fetchone()[0]
    passes = conn.execute(
        """
        SELECT COUNT(*)
        FROM swipes
        WHERE action = 'pass'
        """
    ).fetchone()[0]
    matches = conn.execute(
        "SELECT COUNT(*) FROM matches"
    ).fetchone()[0]
    messages = conn.execute(
        "SELECT COUNT(*) FROM messages"
    ).fetchone()[0]
    reports = conn.execute(
        """
        SELECT COUNT(*)
        FROM reports
        WHERE status = 'open'
        """
    ).fetchone()[0]
    today = now_utc().strftime(
        "%Y-%m-%d"
    )
    new_today = conn.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE created_at LIKE ?
        """,
        (today + "%",),
    ).fetchone()[0]
    likes_today = conn.execute(
        """
        SELECT COUNT(*)
        FROM swipes
        WHERE action = 'like'
          AND created_at LIKE ?
        """,
        (today + "%",),
    ).fetchone()[0]
    matches_today = conn.execute(
        """
        SELECT COUNT(*)
        FROM matches
        WHERE created_at LIKE ?
        """,
        (today + "%",),
    ).fetchone()[0]
    messages_today = conn.execute(
        """
        SELECT COUNT(*)
        FROM messages
        WHERE created_at LIKE ?
        """,
        (today + "%",),
    ).fetchone()[0]
    reports_today = conn.execute(
        """
        SELECT COUNT(*)
        FROM reports
        WHERE created_at LIKE ?
        """,
        (today + "%",),
    ).fetchone()[0]
    conn.close()
    await update.message.reply_text(
        "👑 <b>TOGETHR ADMIN PANEL</b>\n\n"
        f"👥 Users: <b>{users}</b>\n"
        f"🟢 Active 7d: <b>{active}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n\n"
        f"❤️ Likes: <b>{likes}</b>\n"
        f"⭐ Super Likes: <b>{super_likes}</b>\n"
        f"❌ Passes: <b>{passes}</b>\n"
        f"🎉 Matches: <b>{matches}</b>\n"
        f"💬 Messages: <b>{messages}</b>\n"
        f"🚨 Open Reports: <b>{reports}</b>\n\n"
        "📅 <b>Այսօր</b>\n"
        f"👤 New users: <b>{new_today}</b>\n"
        f"❤️ Likes: <b>{likes_today}</b>\n"
        f"🎉 Matches: <b>{matches_today}</b>\n"
        f"💬 Messages: <b>{messages_today}</b>\n"
        f"🚨 Reports: <b>{reports_today}</b>",
        parse_mode="HTML",
    )
async def users_command(update, context):
    if not admin_only(update.effective_user.id):
        return
    conn = get_db()
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
        await update.message.reply_text(
            "Users չկան։"
        )
        return
    lines = [
        "👥 <b>Վերջին 30 օգտատերերը</b>\n"
    ]
    for user in rows:
        lines.append(
            f"• {html.escape(user['name'] or '—')} "
            f"| ID: <code>{user['id']}</code> "
            f"| @{html.escape(user['username'] or '—')} "
            f"| {html.escape(user['city'] or '—')} "
            f"| {'🚫' if user['banned'] else '🟢'}"
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )
async def reports_command(update, context):
    if not admin_only(update.effective_user.id):
        return
    conn = get_db()
    reports = conn.execute(
        """
        SELECT *
        FROM reports
        WHERE status = 'open'
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    conn.close()
    if not reports:
        await update.message.reply_text(
            "✅ Բաց Report-ներ չկան։"
        )
        return
    lines = [
        "🚨 <b>Բաց Report-ներ</b>\n"
    ]
    for report in reports:
        reporter = get_user(
            report["reporter"]
        )
        reported = get_user(
            report["reported"]
        )
        lines.append(
            f"🚨 <b>#{report['id']}</b>\n"
            f"Reporter: {html.escape(user_label(reporter))}\n"
            f"Reported: {html.escape(user_label(reported))}\n"
            f"Reason: {html.escape(report['reason'] or '—')}\n"
            f"Date: {html.escape(report['created_at'] or '—')}\n"
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
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
        target_id = int(
            context.args[0]
        )
    except ValueError:
        await update.message.reply_text(
            "❌ Սխալ USER_ID։"
        )
        return
    target = get_user(target_id)
    if not target:
        await update.message.reply_text(
            "❌ User-ը չի գտնվել։"
        )
        return
    update_user(
        target_id,
        banned=1,
    )
    await update.message.reply_text(
        f"🚫 User {target_id} banned."
    )
    await log_admin(
        context,
        update.effective_user.id,
        "ADMIN_BAN",
        (
            f"Target: {user_label(target)}"
        ),
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "🚫 Քո Togethr հաշիվը "
                "արգելափակվել է Admin-ի կողմից։"
            ),
        )
    except Exception:
        pass
async def unban_command(update, context):
    if not admin_only(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Օգտագործում՝ /unban USER_ID"
        )
        return
    try:
        target_id = int(
            context.args[0]
        )
    except ValueError:
        await update.message.reply_text(
            "❌ Սխալ USER_ID։"
        )
        return
    target = get_user(target_id)
    if not target:
        await update.message.reply_text(
            "❌ User-ը չի գտնվել։"
        )
        return
    update_user(
        target_id,
        banned=0,
    )
    await update.message.reply_text(
        f"✅ User {target_id} unbanned."
    )
    await log_admin(
        context,
        update.effective_user.id,
        "ADMIN_UNBAN",
        (
            f"Target: {user_label(target)}"
        ),
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "✅ Քո Togethr հաշիվը "
                "ապաշրջափակվել է։"
            ),
        )
    except Exception:
        pass
# ============================================================
# ADMIN: RECENT ACTIVITY
# ============================================================
async def activity_command(update, context):
    if not admin_only(update.effective_user.id):
        return
    conn = get_db()
    rows = conn.execute(
        """
        SELECT *
        FROM activity_logs
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text(
            "Activity logs չկան։"
        )
        return
    lines = [
        "📡 <b>Վերջին 30 Activity</b>\n"
    ]
    for row in rows:
        user = get_user(
            row["user_id"]
        )
        label = (
            user_label(user)
            if user
            else f"ID: {row['user_id']}"
        )
        lines.append(
            f"⚡ <b>{html.escape(row['action'])}</b>\n"
            f"👤 {html.escape(label)}\n"
            f"📝 {html.escape(row['details'] or '')}\n"
            f"🕐 {html.escape(row['created_at'])}\n"
        )
    # Telegram message limit protection
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n\n..."
    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )
# ============================================================
# ERROR HANDLER
# ============================================================
async def error_handler(update, context):
    logger.exception(
        "Exception while handling update:",
        exc_info=context.error,
    )
    error_text = str(context.error)
    if ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "🚨 <b>BOT ERROR</b>\n\n"
                    f"<code>{html.escape(error_text[:3500])}</code>\n\n"
                    f"🕐 {now_str()}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
# ============================================================
# MAIN
# ============================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )
    init_database()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "users",
            users_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "reports",
            reports_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "ban",
            ban_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "unban",
            unban_command,
        )
    )
    application.add_handler(
        CommandHandler(
            "activity",
            activity_command,
        )
    )
    # Photo handler
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            profile_photo_handler,
        )
    )
    # Callback handler
    application.add_handler(
        CallbackQueryHandler(
            callback_router,
        )
    )
    # Text handler
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )
    application.add_error_handler(
        error_handler
    )
    logger.info(
        "Togethr bot started successfully."
    )
    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )
if __name__ == "__main__":
    main()

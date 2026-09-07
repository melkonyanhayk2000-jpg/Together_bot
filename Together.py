import os
import html
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

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
    ContextTypes,
    filters,
)

BOT_NAME = "Together"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
INACTIVITY_SECONDS = 180

BASE_DIR = "/data" if os.path.isdir("/data") else os.path.join(os.getcwd(), "data")
os.makedirs(BASE_DIR, exist_ok=True)
DB_PATH = os.path.join(BASE_DIR, "together.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(BOT_NAME)

PROFILE_STEPS = ("name", "age", "city", "gender", "looking_for", "about", "photo")


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            name TEXT NOT NULL,
            age INTEGER NOT NULL,
            city TEXT NOT NULL,
            gender TEXT NOT NULL,
            looking_for TEXT NOT NULL,
            about TEXT DEFAULT '',
            photo_file_id TEXT DEFAULT '',
            banned INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS swipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(from_user, to_user)
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blocker INTEGER NOT NULL,
            blocked INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(blocker, blocked)
        );

        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_sessions (
            user_id INTEGER PRIMARY KEY,
            match_id INTEGER NOT NULL,
            last_active TEXT NOT NULL
        );

        INSERT OR IGNORE INTO bot_settings(key, value)
        VALUES ('activity_notifications', '1');
        """)


def user_exists(uid):
    with db() as con:
        return con.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None


def get_user(uid):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def touch(uid):
    with db() as con:
        con.execute("UPDATE users SET last_active=? WHERE id=?", (now(), uid))


def log_activity(uid, action, details=""):
    with db() as con:
        con.execute(
            "INSERT INTO activity_logs(user_id,action,details,created_at) VALUES(?,?,?,?)",
            (uid, action, details, now()),
        )


def activity_notifications_enabled():
    with db() as con:
        row = con.execute(
            "SELECT value FROM bot_settings WHERE key='activity_notifications'"
        ).fetchone()
    return bool(row and row["value"] == "1")


async def admin_activity(app, uid, action, details=""):
    log_activity(uid, action, details)
    if ADMIN_ID and activity_notifications_enabled():
        try:
            await app.bot.send_message(
                ADMIN_ID,
                f"📊 <b>{BOT_NAME}</b>\n"
                f"👤 ID: <code>{uid}</code>\n"
                f"⚡ {html.escape(action)}"
                + (f"\n📝 {html.escape(details)}" if details else ""),
                parse_mode="HTML",
            )
        except Exception:
            log.exception("Admin notification failed")


def main_keyboard(uid):
    rows = [
        ["👤 Իմ պրոֆիլը", "🔎 Գտնել մարդկանց"],
        ["❤️ Իմ Match-երը", "✏️ Խմբագրել պրոֆիլը"],
        ["🗑️ Ջնջել պրոֆիլը"],
    ]
    if uid == ADMIN_ID:
        rows.append(["🛡️ Admin մենյու"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def start_keyboard():
    return ReplyKeyboardMarkup([["🚀 Ստեղծել պրոֆիլ"]], resize_keyboard=True)


def profile_text(u):
    return (
        f"👤 <b>{html.escape(u['name'])}</b>\n"
        f"🎂 {u['age']}\n"
        f"📍 {html.escape(u['city'])}\n"
        f"⚧ {html.escape(u['gender'])}\n"
        f"❤️ Փնտրում է՝ {html.escape(u['looking_for'])}\n"
        f"📝 {html.escape(u['about'] or 'Չի նշվել')}"
    )


def profile_buttons(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Խմբագրել", callback_data="edit_profile")],
        [InlineKeyboardButton("🗑️ Ջնջել պրոֆիլը", callback_data="delete_profile")],
    ])


def discovery_buttons(target):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❤️ Հավանել", callback_data=f"like:{target}"),
            InlineKeyboardButton("❌ Անցնել", callback_data=f"pass:{target}"),
        ],
        [
            InlineKeyboardButton("⭐ Super Like", callback_data=f"super:{target}"),
            InlineKeyboardButton("🚫 Արգելափակել", callback_data=f"block:{target}"),
        ],
        [InlineKeyboardButton("⚠️ Բողոքել", callback_data=f"report:{target}")],
    ])


def match_buttons(match_id, other):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Բացել չատը", callback_data=f"chat:{match_id}")],
        [InlineKeyboardButton("🚫 Արգելափակել", callback_data=f"block:{other}")],
        [InlineKeyboardButton("⚠️ Բողոքել", callback_data=f"report:{other}")],
    ])


def parse_age(text):
    try:
        age = int(text.strip())
        return age if 18 <= age <= 100 else None
    except ValueError:
        return None


async def send_profile_prompt(update, context, editing=False):
    context.user_data["profile_step"] = "name"
    context.user_data["editing"] = editing
    await update.effective_message.reply_text(
        "✏️ Սկսենք պրոֆիլից։\n\nԻնչպե՞ս է քո անունը։",
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_profile_step(update, context):
    uid = update.effective_user.id
    step = context.user_data.get("profile_step")
    if not step:
        return False

    text = (update.effective_message.text or "").strip()
    data = context.user_data.setdefault("profile", {})

    if step == "name":
        if len(text) < 2 or len(text) > 40:
            await update.effective_message.reply_text("❗ Գրիր անունը՝ 2-40 նիշ։")
            return True
        data["name"] = text
        context.user_data["profile_step"] = "age"
        await update.effective_message.reply_text("🎂 Քանի՞ տարեկան ես։")
        return True

    if step == "age":
        age = parse_age(text)
        if not age:
            await update.effective_message.reply_text("❗ Տարիքը պետք է լինի 18-100։")
            return True
        data["age"] = age
        context.user_data["profile_step"] = "city"
        await update.effective_message.reply_text("📍 Ո՞ր քաղաքում ես։")
        return True

    if step == "city":
        if len(text) < 2:
            await update.effective_message.reply_text("❗ Գրիր քաղաքը։")
            return True
        data["city"] = text[:60]
        context.user_data["profile_step"] = "gender"
        await update.effective_message.reply_text(
            "⚧ Ընտրիր սեռը՝\n\n"
            "👨 Տղամարդ\n"
            "👩 Կին"
        )
        return True

    if step == "gender":
        if text not in ("👨 Տղամարդ", "👩 Կին"):
            await update.effective_message.reply_text("Ընտրիր՝ 👨 Տղամարդ կամ 👩 Կին։")
            return True
        data["gender"] = "Տղամարդ" if "Տղամարդ" in text else "Կին"
        context.user_data["profile_step"] = "looking_for"
        await update.effective_message.reply_text(
            "❤️ Ո՞ւմ ես ցանկանում գտնել՝\n\n"
            "👨 Տղամարդկանց\n"
            "👩 Կանանց\n"
            "👨‍👩‍👧 Բոլորին"
        )
        return True

    if step == "looking_for":
        if text not in ("👨 Տղամարդկանց", "👩 Կանանց", "👨‍👩‍👧 Բոլորին"):
            await update.effective_message.reply_text(
                "Ընտրիր՝ 👨 Տղամարդկանց, 👩 Կանանց կամ 👨‍👩‍👧 Բոլորին։"
            )
            return True
        data["looking_for"] = (
            "Տղամարդ" if text == "👨 Տղամարդկանց"
            else "Կին" if text == "👩 Կանանց"
            else "Բոլորին"
        )
        context.user_data["profile_step"] = "about"
        await update.effective_message.reply_text(
            "📝 Մի փոքր պատմիր քո մասին։\n"
            "Կարող ես գրել մինչև 500 նիշ։"
        )
        return True

    if step == "about":
        data["about"] = text[:500]
        context.user_data["profile_step"] = "photo"
        await update.effective_message.reply_text("📸 Ուղարկիր քո լուսանկարը։")
        return True

    return False


async def handle_photo(update, context):
    if context.user_data.get("profile_step") != "photo":
        return False

    uid = update.effective_user.id
    data = context.user_data.setdefault("profile", {})
    data["photo_file_id"] = update.effective_message.photo[-1].file_id

    with db() as con:
        old = con.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone()
        if old:
            con.execute("""
                UPDATE users
                SET username=?, name=?, age=?, city=?, gender=?, looking_for=?,
                    about=?, photo_file_id=?, last_active=?
                WHERE id=?
            """, (
                update.effective_user.username or "",
                data["name"], data["age"], data["city"], data["gender"],
                data["looking_for"], data["about"], data["photo_file_id"], now(), uid
            ))
        else:
            con.execute("""
                INSERT INTO users
                (id,username,name,age,city,gender,looking_for,about,photo_file_id,created_at,last_active)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (
                uid, update.effective_user.username or "", data["name"], data["age"],
                data["city"], data["gender"], data["looking_for"], data["about"],
                data["photo_file_id"], now(), now()
            ))

    context.user_data.clear()
    await update.effective_message.reply_text(
        "🎉 <b>Պրոֆիլը պատրաստ է։</b>\n\nԲարի գալուստ Together ❤️",
        parse_mode="HTML",
        reply_markup=main_keyboard(uid),
    )
    await admin_activity(context.application, uid, "Պրոֆիլը ստեղծվեց/թարմացվեց")
    return True


def compatible(me, other):
    if me["looking_for"] != "Բոլորին" and me["looking_for"] != other["gender"]:
        return False
    if other["looking_for"] != "Բոլորին" and other["looking_for"] != me["gender"]:
        return False
    return True


def blocked_either(a, b):
    with db() as con:
        return con.execute("""
            SELECT 1 FROM blocks
            WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)
        """, (a, b, b, a)).fetchone() is not None


def already_swiped(a, b):
    with db() as con:
        return con.execute(
            "SELECT 1 FROM swipes WHERE from_user=? AND to_user=?",
            (a, b)
        ).fetchone() is not None


def find_candidate(uid):
    me = get_user(uid)
    if not me:
        return None

    with db() as con:
        rows = con.execute("""
            SELECT * FROM users
            WHERE id != ? AND banned=0
              AND id NOT IN (
                SELECT to_user FROM swipes WHERE from_user=?
              )
              AND id NOT IN (
                SELECT blocked FROM blocks WHERE blocker=?
              )
              AND id NOT IN (
                SELECT blocker FROM blocks WHERE blocked=?
              )
            ORDER BY last_active DESC
            LIMIT 100
        """, (uid, uid, uid, uid)).fetchall()

    candidates = [u for u in rows if compatible(me, u)]
    if not candidates:
        return None

    def score(u):
        city = 30 if u["city"].lower() == me["city"].lower() else 0
        age = max(0, 20 - abs(u["age"] - me["age"]))
        try:
            activity = max(0, 10 - int(
                (datetime.now(timezone.utc) -
                 datetime.fromisoformat(u["last_active"])).total_seconds() / 86400
            ))
        except Exception:
            activity = 0
        return city + age + activity

    return max(candidates, key=score)


async def show_candidate(update, uid):
    candidate = find_candidate(uid)
    if not candidate:
        await update.effective_message.reply_text(
            "🔎 Այս պահին համապատասխան նոր պրոֆիլ չկա։\nՓորձիր մի փոքր ուշ։",
            reply_markup=main_keyboard(uid),
        )
        return

    text = (
        f"❤️ <b>{html.escape(candidate['name'])}</b>, {candidate['age']}\n"
        f"📍 {html.escape(candidate['city'])}\n\n"
        f"{html.escape(candidate['about'] or '')}"
    )
    if candidate["photo_file_id"]:
        await update.effective_message.reply_photo(
            candidate["photo_file_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=discovery_buttons(candidate["id"]),
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=discovery_buttons(candidate["id"])
        )


async def process_swipe(update, context, action, target):
    uid = update.effective_user.id
    if uid == target or not user_exists(target) or blocked_either(uid, target):
        await update.callback_query.answer("Այս պրոֆիլը հասանելի չէ։", show_alert=True)
        return

    with db() as con:
        con.execute("""
            INSERT OR REPLACE INTO swipes(from_user,to_user,action,created_at)
            VALUES(?,?,?,?)
        """, (uid, target, action, now()))

        mutual = con.execute("""
            SELECT action FROM swipes
            WHERE from_user=? AND to_user=?
        """, (target, uid)).fetchone()

        is_match = action in ("like", "super") and mutual and mutual["action"] in ("like", "super")
        match_id = None
        if is_match:
            a, b = sorted((uid, target))
            con.execute(
                "INSERT OR IGNORE INTO matches(user1,user2,created_at) VALUES(?,?,?)",
                (a, b, now())
            )
            row = con.execute(
                "SELECT id FROM matches WHERE user1=? AND user2=?", (a, b)
            ).fetchone()
            match_id = row["id"]

    if is_match:
        try:
            await context.bot.send_message(
                target,
                "🎉 <b>Նոր Match Together-ում!</b>\n\n"
                "Դուք երկուսդ էլ հավանել եք միմյանց ❤️",
                parse_mode="HTML",
                reply_markup=main_keyboard(target),
            )
        except Exception:
            pass

        await update.callback_query.message.reply_text(
            "🎉 <b>Match!</b>\n\nԴուք հավանել եք միմյանց ❤️",
            parse_mode="HTML",
            reply_markup=match_buttons(match_id, target),
        )
    else:
        await update.callback_query.message.reply_text(
            "❤️ Հաջողվեց։ Շարունակե՞նք։",
            reply_markup=main_keyboard(uid),
        )

    await update.callback_query.answer()
    await admin_activity(context.application, uid, action, f"target={target}")


def get_matches(uid):
    with db() as con:
        return con.execute("""
            SELECT m.id,
                   CASE WHEN m.user1=? THEN m.user2 ELSE m.user1 END AS other_id
            FROM matches m
            WHERE m.user1=? OR m.user2=?
            ORDER BY m.created_at DESC
        """, (uid, uid, uid)).fetchall()


def get_match(uid, match_id):
    with db() as con:
        return con.execute("""
            SELECT * FROM matches
            WHERE id=? AND (user1=? OR user2=?)
        """, (match_id, uid, uid)).fetchone()


def set_chat(uid, match_id):
    with db() as con:
        con.execute("""
            INSERT INTO chat_sessions(user_id,match_id,last_active)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                match_id=excluded.match_id,
                last_active=excluded.last_active
        """, (uid, match_id, now()))


def clear_chat(uid):
    with db() as con:
        con.execute("DELETE FROM chat_sessions WHERE user_id=?", (uid,))


def chat_session(uid):
    with db() as con:
        return con.execute(
            "SELECT * FROM chat_sessions WHERE user_id=?", (uid,)
        ).fetchone()


async def open_chat(update, uid, match_id):
    match = get_match(uid, match_id)
    if not match:
        await update.effective_message.reply_text("❗ Match-ը չի գտնվել։")
        return

    other = match["user2"] if match["user1"] == uid else match["user1"]
    if blocked_either(uid, other):
        await update.effective_message.reply_text("🚫 Չատը հասանելի չէ։")
        return

    set_chat(uid, match_id)
    await update.effective_message.reply_text(
        "💬 <b>Չատը բացված է</b>\n\n"
        "Գրիր հաղորդագրություն։\n"
        "⏱️ 3 րոպե անգործությունից չատը ավտոմատ կփակվի։\n\n"
        "❌ Փակել չատը՝ /cancel",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([["❌ Փակել չատը"]], resize_keyboard=True),
    )


async def forward_chat_message(update, uid, text, context):
    session = chat_session(uid)
    if not session:
        return False

    match = get_match(uid, session["match_id"])
    if not match:
        clear_chat(uid)
        return False

    other = match["user2"] if match["user1"] == uid else match["user1"]
    if blocked_either(uid, other):
        clear_chat(uid)
        await update.effective_message.reply_text(
            "🚫 Չատը փակվել է։", reply_markup=main_keyboard(uid)
        )
        return True

    with db() as con:
        con.execute(
            "INSERT INTO messages(match_id,sender_id,text,created_at) VALUES(?,?,?,?)",
            (session["match_id"], uid, text[:4000], now()),
        )
        con.execute(
            "UPDATE chat_sessions SET last_active=? WHERE user_id=?",
            (now(), uid),
        )

    try:
        await context.bot.send_message(
            other,
            f"💬 <b>Նոր հաղորդագրություն Together-ում</b>\n\n"
            f"{html.escape(text[:4000])}",
            parse_mode="HTML",
            reply_markup=main_keyboard(other),
        )
    except Exception:
        pass
    return True


async def delete_profile(update, uid):
    with db() as con:
        match_rows = con.execute("""
            SELECT id FROM matches WHERE user1=? OR user2=?
        """, (uid, uid)).fetchall()
        match_ids = [r["id"] for r in match_rows]

        for mid in match_ids:
            con.execute("DELETE FROM messages WHERE match_id=?", (mid,))

        con.execute("DELETE FROM chat_sessions WHERE user_id=?", (uid,))
        con.execute("DELETE FROM swipes WHERE from_user=? OR to_user=?", (uid, uid))
        con.execute("DELETE FROM matches WHERE user1=? OR user2=?", (uid, uid))
        con.execute("DELETE FROM reports WHERE reporter=? OR reported=?", (uid, uid))
        con.execute("DELETE FROM blocks WHERE blocker=? OR blocked=?", (uid, uid))
        con.execute("DELETE FROM activity_logs WHERE user_id=?", (uid,))
        con.execute("DELETE FROM users WHERE id=?", (uid,))


async def profile_delete_confirm(update, context):
    await update.effective_message.reply_text(
        "⚠️ <b>Ջնջե՞լ պրոֆիլը</b>\n\n"
        "Բոլոր տվյալները, Match-երը, Like-երը և չատերի տվյալները կջնջվեն։\n"
        "Այս գործողությունը հնարավոր չէ հետարկել։",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Այո, ջնջել", callback_data="delete_yes")],
            [InlineKeyboardButton("❌ Չեղարկել", callback_data="profile")],
        ]),
    )


async def show_matches(update, uid):
    matches = get_matches(uid)
    if not matches:
        await update.effective_message.reply_text(
            "❤️ Դեռ Match չունես։",
            reply_markup=main_keyboard(uid),
        )
        return

    await update.effective_message.reply_text("❤️ <b>Իմ Match-երը</b>", parse_mode="HTML")
    for m in matches:
        other = get_user(m["other_id"])
        if not other:
            continue
        await update.effective_message.reply_text(
            f"❤️ <b>{html.escape(other['name'])}</b>, {other['age']}\n"
            f"📍 {html.escape(other['city'])}",
            parse_mode="HTML",
            reply_markup=match_buttons(m["id"], other["id"]),
        )


async def report_menu(update, target):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Անպատշաճ բովանդակություն", callback_data=f"report_reason:{target}:Անպատշաճ բովանդակություն")],
        [InlineKeyboardButton("🤖 Կեղծ պրոֆիլ", callback_data=f"report_reason:{target}:Կեղծ պրոֆիլ")],
        [InlineKeyboardButton("⚠️ Այլ", callback_data=f"report_reason:{target}:Այլ")],
    ])


async def admin_menu(update):
    enabled = activity_notifications_enabled()
    toggle = (
        "🔕 Անջատել ակտիվության հաղորդագրությունները"
        if enabled else "🔔 Միացնել ակտիվության հաղորդագրությունները"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Վիճակագրություն", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Վերջին օգտատերեր", callback_data="admin_users")],
        [InlineKeyboardButton("⚠️ Բողոքներ", callback_data="admin_reports")],
        [InlineKeyboardButton(toggle, callback_data="admin_toggle_activity")],
        [InlineKeyboardButton("💾 Backup", callback_data="admin_backup")],
    ])
    await update.effective_message.reply_text(
        f"🛡️ <b>{BOT_NAME} Admin</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def admin_stats(update):
    with db() as con:
        users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        matches = con.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"]
        reports = con.execute(
            "SELECT COUNT(*) c FROM reports WHERE status='new'"
        ).fetchone()["c"]
        active = con.execute("""
            SELECT COUNT(*) c FROM users
            WHERE last_active >= datetime('now','-1 day')
        """).fetchone()["c"]
    await update.callback_query.message.reply_text(
        f"📊 <b>{BOT_NAME}</b>\n\n"
        f"👥 Օգտատերեր՝ {users}\n"
        f"🟢 Ակտիվ 24ժ՝ {active}\n"
        f"❤️ Match-եր՝ {matches}\n"
        f"⚠️ Նոր բողոքներ՝ {reports}",
        parse_mode="HTML",
    )


async def admin_users(update):
    with db() as con:
        rows = con.execute("""
            SELECT id,name,age,city,last_active
            FROM users ORDER BY created_at DESC LIMIT 20
        """).fetchall()
    if not rows:
        await update.callback_query.message.reply_text("Օգտատերեր չկան։")
        return
    text = "👥 <b>Վերջին օգտատերերը</b>\n\n"
    for u in rows:
        text += (
            f"• {html.escape(u['name'])}, {u['age']} — "
            f"{html.escape(u['city'])} — <code>{u['id']}</code>\n"
        )
    await update.callback_query.message.reply_text(text, parse_mode="HTML")


async def admin_reports(update):
    with db() as con:
        rows = con.execute("""
            SELECT * FROM reports
            WHERE status='new' ORDER BY created_at DESC LIMIT 30
        """).fetchall()
    if not rows:
        await update.callback_query.message.reply_text("⚠️ Նոր բողոքներ չկան։")
        return
    text = "⚠️ <b>Բողոքներ</b>\n\n"
    for r in rows:
        text += (
            f"#{r['id']} | reporter=<code>{r['reporter']}</code> | "
            f"reported=<code>{r['reported']}</code>\n"
            f"Պատճառ՝ {html.escape(r['reason'])}\n\n"
        )
    await update.callback_query.message.reply_text(text, parse_mode="HTML")


async def admin_backup(update):
    filename = os.path.join(
        BACKUP_DIR,
        f"together_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    )
    src = db()
    try:
        dest = sqlite3.connect(filename)
        with dest:
            src.backup(dest)
        dest.close()
    finally:
        src.close()
    await update.callback_query.message.reply_text(
        f"💾 Backup ստեղծվեց։\n<code>{html.escape(filename)}</code>",
        parse_mode="HTML",
    )


async def inactivity_cleanup_loop(app):
    while True:
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - INACTIVITY_SECONDS
            expired = []
            with db() as con:
                rows = con.execute("SELECT * FROM chat_sessions").fetchall()
                for row in rows:
                    try:
                        ts = datetime.fromisoformat(row["last_active"]).timestamp()
                    except Exception:
                        ts = 0
                    if ts < cutoff:
                        expired.append(row["user_id"])
                for uid in expired:
                    con.execute("DELETE FROM chat_sessions WHERE user_id=?", (uid,))

            for uid in expired:
                try:
                    await app.bot.send_message(
                        uid,
                        "⏱️ <b>Չատը ավտոմատ փակվեց</b>\n\n"
                        "3 րոպե անգործության պատճառով։",
                        parse_mode="HTML",
                        reply_markup=main_keyboard(uid),
                    )
                except Exception:
                    pass
        except Exception:
            log.exception("Cleanup error")
        await asyncio.sleep(30)


async def start(update, context):
    uid = update.effective_user.id
    context.user_data.clear()

    u = get_user(uid)
    if u:
        touch(uid)
        await update.effective_message.reply_text(
            f"Բարի վերադարձ <b>{html.escape(u['name'])}</b> ❤️\n\n"
            f"Դու Together-ում ես։",
            parse_mode="HTML",
            reply_markup=main_keyboard(uid),
        )
        return

    await update.effective_message.reply_text(
        f"❤️ <b>Բարի գալուստ {BOT_NAME}</b>\n\n"
        "Ծանոթացիր նոր մարդկանց, գտիր փոխադարձ համակրանք և սկսիր շփվել։",
        parse_mode="HTML",
        reply_markup=start_keyboard(),
    )


async def help_cmd(update, context):
    await update.effective_message.reply_text(
        f"❤️ <b>{BOT_NAME}</b>\n\n"
        "🔎 Գտնել մարդկանց — նոր պրոֆիլներ\n"
        "❤️ Իմ Match-երը — փոխադարձ հավանումներ\n"
        "✏️ Խմբագրել պրոֆիլը — փոխել տվյալները\n"
        "🗑️ Ջնջել պրոֆիլը — ամբողջական ջնջում\n"
        "❌ /cancel — չեղարկել ընթացիկ գործողությունը",
        parse_mode="HTML",
        reply_markup=main_keyboard(update.effective_user.id)
        if user_exists(update.effective_user.id) else start_keyboard(),
    )


async def cancel(update, context):
    uid = update.effective_user.id
    clear_chat(uid)
    context.user_data.clear()
    if user_exists(uid):
        await update.effective_message.reply_text(
            "❌ Գործողությունը չեղարկվեց։",
            reply_markup=main_keyboard(uid),
        )
    else:
        await update.effective_message.reply_text(
            "❌ Չեղարկվեց։",
            reply_markup=start_keyboard(),
        )


async def callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    touch(uid)

    data = q.data

    if data == "profile":
        u = get_user(uid)
        if not u:
            await q.message.reply_text("Պրոֆիլ դեռ չունես։", reply_markup=start_keyboard())
            return
        text = profile_text(u)
        if u["photo_file_id"]:
            await q.message.reply_photo(
                u["photo_file_id"], caption=text, parse_mode="HTML",
                reply_markup=profile_buttons(uid)
            )
        else:
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=profile_buttons(uid))
        return

    if data == "edit_profile":
        await q.message.reply_text("✏️ Խմբագրենք պրոֆիլը։")
        context.user_data.clear()
        await send_profile_prompt(update, context, editing=True)
        return

    if data == "delete_profile":
        await profile_delete_confirm(update, uid)
        return

    if data == "delete_yes":
        if user_exists(uid):
            await delete_profile(update, uid)
        context.user_data.clear()
        await q.message.reply_text(
            "🗑️ <b>Պրոֆիլը ամբողջությամբ ջնջվեց։</b>\n\n"
            "Եթե ցանկանաս, կարող ես ստեղծել նոր պրոֆիլ։",
            parse_mode="HTML",
            reply_markup=start_keyboard(),
        )
        return

    if data.startswith(("like:", "pass:", "super:")):
        action, target = data.split(":")
        await process_swipe(update, context, action, int(target))
        return

    if data.startswith("chat:"):
        await open_chat(update, uid, int(data.split(":")[1]))
        return

    if data.startswith("block:"):
        target = int(data.split(":")[1])
        if target != uid and user_exists(target):
            with db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO blocks(blocker,blocked,created_at) VALUES(?,?,?)",
                    (uid, target, now()),
                )
                con.execute(
                    "DELETE FROM matches WHERE (user1=? AND user2=?) OR (user1=? AND user2=?)",
                    (uid, target, target, uid),
                )
            clear_chat(uid)
            await q.message.reply_text(
                "🚫 Օգտատերը արգելափակվեց։",
                reply_markup=main_keyboard(uid),
            )
            await admin_activity(context.application, uid, "Օգտատերը արգելափակեց", str(target))
        return

    if data.startswith("report:"):
        target = int(data.split(":")[1])
        await q.message.reply_text(
            "⚠️ Ընտրիր բողոքի պատճառը։",
            reply_markup=await report_menu(update, target),
        )
        return

    if data.startswith("report_reason:"):
        _, target, reason = data.split(":", 2)
        target = int(target)
        with db() as con:
            con.execute(
                "INSERT INTO reports(reporter,reported,reason,created_at) VALUES(?,?,?,?)",
                (uid, target, reason, now()),
            )
        await q.message.reply_text(
            "✅ Բողոքը ուղարկվեց։ Շնորհակալություն։",
            reply_markup=main_keyboard(uid),
        )
        await admin_activity(context.application, uid, "Բողոք ուղարկվեց", f"{target}: {reason}")
        return

    if data == "admin_stats" and uid == ADMIN_ID:
        await admin_stats(update)
        return

    if data == "admin_users" and uid == ADMIN_ID:
        await admin_users(update)
        return

    if data == "admin_reports" and uid == ADMIN_ID:
        await admin_reports(update)
        return

    if data == "admin_backup" and uid == ADMIN_ID:
        await admin_backup(update)
        return

    if data == "admin_toggle_activity" and uid == ADMIN_ID:
        new_value = "0" if activity_notifications_enabled() else "1"
        with db() as con:
            con.execute("""
                INSERT INTO bot_settings(key,value) VALUES('activity_notifications',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (new_value,))
        await q.message.reply_text(
            "🔔 Ակտիվության հաղորդագրությունները "
            + ("միացված են։" if new_value == "1" else "անջատված են։")
        )
        return


async def text_router(update, context):
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()

    # Main menu buttons always have priority.
    if text == "🚀 Ստեղծել պրոֆիլ":
        context.user_data.clear()
        await send_profile_prompt(update, context)
        return

    if text == "👤 Իմ պրոֆիլը":
        if not user_exists(uid):
            await update.effective_message.reply_text("Սկզբում ստեղծիր պրոֆիլ։", reply_markup=start_keyboard())
            return
        u = get_user(uid)
        if u["photo_file_id"]:
            await update.effective_message.reply_photo(
                u["photo_file_id"], caption=profile_text(u), parse_mode="HTML",
                reply_markup=profile_buttons(uid)
            )
        else:
            await update.effective_message.reply_text(
                profile_text(u), parse_mode="HTML", reply_markup=profile_buttons(uid)
            )
        return

    if text == "🔎 Գտնել մարդկանց":
        if not user_exists(uid):
            await update.effective_message.reply_text("Սկզբում ստեղծիր պրոֆիլ։", reply_markup=start_keyboard())
            return
        touch(uid)
        await show_candidate(update, uid)
        return

    if text == "❤️ Իմ Match-երը":
        if user_exists(uid):
            await show_matches(update, uid)
        return

    if text == "✏️ Խմբագրել պրոֆիլը":
        if user_exists(uid):
            context.user_data.clear()
            await send_profile_prompt(update, context, editing=True)
        return

    if text == "🗑️ Ջնջել պրոֆիլը":
        if user_exists(uid):
            await profile_delete_confirm(update, context)
        return

    if text == "🛡️ Admin մենյու" and uid == ADMIN_ID:
        await admin_menu(update)
        return

    if text == "❌ Փակել չատը":
        clear_chat(uid)
        await update.effective_message.reply_text(
            "❌ Չատը փակվեց։",
            reply_markup=main_keyboard(uid),
        )
        return

    if await forward_chat_message(update, uid, text, context):
        return

    if context.user_data.get("profile_step"):
        handled = await handle_profile_step(update, context)
        if handled:
            touch(uid)
            return

    if user_exists(uid):
        touch(uid)
        await update.effective_message.reply_text(
            "Ընտրիր գործողությունը մենյուից։",
            reply_markup=main_keyboard(uid),
        )
    else:
        await update.effective_message.reply_text(
            "Սկզբում ստեղծիր պրոֆիլ։",
            reply_markup=start_keyboard(),
        )


async def photo_router(update, context):
    if await handle_photo(update, context):
        return
    uid = update.effective_user.id
    if user_exists(uid):
        await update.effective_message.reply_text(
            "Այս պահին լուսանկար պետք չէ։",
            reply_markup=main_keyboard(uid),
        )


async def admin_cmd(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    await admin_menu(update)


async def ban_cmd(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Օգտագործում՝ /ban USER_ID")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Սխալ ID։")
        return
    with db() as con:
        con.execute("UPDATE users SET banned=1 WHERE id=?", (target,))
    await update.effective_message.reply_text(f"🚫 Արգելափակված՝ {target}")


async def unban_cmd(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Օգտագործում՝ /unban USER_ID")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Սխալ ID։")
        return
    with db() as con:
        con.execute("UPDATE users SET banned=0 WHERE id=?", (target,))
    await update.effective_message.reply_text(f"✅ Ապաբլոկավորված՝ {target}")


async def post_init(app):
    init_db()
    app.create_task(inactivity_cleanup_loop(app))


async def error_handler(update, context):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")

    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.PHOTO, photo_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)

    log.info("%s started", BOT_NAME)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

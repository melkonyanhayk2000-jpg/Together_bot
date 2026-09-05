import os
import sqlite3
import logging
import html
import shutil
from pathlib import Path
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
# IMPORTANT: put DB_FILE on persistent storage in production.
# Example: DB_FILE=/data/togethr.db
DB_FILE = os.getenv("DB_FILE", "togethr.db")
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "6"))
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "30"))

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("togethr")

APP_STARTED_AT = datetime.now(timezone.utc)


def db_path():
    return Path(DB_FILE).expanduser().resolve()


def backup_dir():
    return Path(BACKUP_DIR).expanduser().resolve()


def db():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def fmt_time(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def init_db():
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
            from_user INTEGER,
            to_user INTEGER,
            action TEXT,
            created_at TEXT,
            UNIQUE(from_user, to_user)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1 INTEGER,
            user2 INTEGER,
            created_at TEXT,
            UNIQUE(user1, user2)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER,
            sender_id INTEGER,
            text TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter INTEGER,
            reported INTEGER,
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
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_active TEXT")
    except sqlite3.OperationalError:
        pass

    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_users_active ON users(last_active)",
        "CREATE INDEX IF NOT EXISTS idx_activity_time ON activity_logs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status)"
    ]:
        cur.execute(sql)

    # Safer SQLite settings for a long-running bot.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=FULL")
    cur.execute("PRAGMA foreign_keys=ON")

    conn.commit()
    conn.close()


def database_integrity_check():
    """Return (ok, message) without modifying user data."""
    try:
        conn = db()
        row = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        result = row[0] if row else "unknown"
        return result == "ok", result
    except Exception as exc:
        logger.exception("Database integrity check failed")
        return False, str(exc)


def create_database_backup(reason="scheduled"):
    """Create a consistent SQLite backup using SQLite's online backup API."""
    source = db_path()
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        return None, "Database file does not exist yet."

    ok, integrity = database_integrity_check()
    if not ok:
        return None, f"Backup cancelled: database integrity check failed: {integrity}"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target = target_dir / f"togethr_{stamp}_{reason}.db"

    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst.commit()
    finally:
        dst.close()
        src.close()

    # Verify the backup before considering it valid.
    check = sqlite3.connect(str(target), timeout=30)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()

    if result != "ok":
        try:
            target.unlink()
        except OSError:
            pass
        return None, f"Backup verification failed: {result}"

    cleanup_old_backups()
    return target, "Backup created and verified successfully."


def cleanup_old_backups():
    directory = backup_dir()
    files = sorted(
        directory.glob("togethr_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[max(BACKUP_KEEP, 1):]:
        try:
            old.unlink()
        except OSError:
            logger.warning("Could not delete old backup: %s", old)


def list_backups(limit=10):
    directory = backup_dir()
    if not directory.exists():
        return []
    return sorted(
        directory.glob("togethr_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]


def backup_scheduler(application):
    """Run periodic backups without requiring the optional PTB job-queue package."""
    import threading

    def run():
        try:
            path, message = create_database_backup("scheduled")
            logger.info("Automatic backup: %s | %s", path or "FAILED", message)
        except Exception:
            logger.exception("Automatic database backup failed")
        finally:
            timer = threading.Timer(
                max(BACKUP_INTERVAL_HOURS, 1) * 3600,
                run,
            )
            timer.daemon = True
            timer.start()

    # First backup shortly after startup, then periodically.
    timer = threading.Timer(10, run)
    timer.daemon = True
    timer.start()


def db_log(user_id, action, details=""):
    conn = db()
    conn.execute(
        "INSERT INTO activity_logs(user_id, action, details, created_at) VALUES(?,?,?,?)",
        (user_id, action, details, now_iso())
    )
    conn.commit()
    conn.close()


def create_user(user):
    conn = db()
    row = conn.execute("SELECT id FROM users WHERE id=?", (user.id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users(id,username,name,banned,created_at,last_active) VALUES(?,?,?,?,?,?)",
            (user.id, user.username, user.first_name, 0, now_iso(), now_iso())
        )
    else:
        conn.execute(
            "UPDATE users SET username=?,last_active=? WHERE id=?",
            (user.username, now_iso(), user.id)
        )
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row


def update_user(user_id, **fields):
    allowed = {
        "username", "name", "age", "city", "gender", "looking_for",
        "about", "photo_file_id", "banned", "last_active"
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [user_id]
    conn = db()
    conn.execute(f"UPDATE users SET {clause} WHERE id=?", values)
    conn.commit()
    conn.close()


def profile_complete(row):
    return bool(row and all([
        row["name"], row["age"], row["city"],
        row["gender"], row["looking_for"],
        row["about"], row["photo_file_id"]
    ]))


def label(user_id):
    row = get_user(user_id)
    if not row:
        return f"ID {user_id}"
    return f"{row['name'] or 'Անուն չկա'} | ID {row['id']}"


def uptime_text():
    delta = datetime.now(timezone.utc) - APP_STARTED_AT
    total = int(delta.total_seconds())
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{d}օր {h}ժ {m}ր"


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Բոտի կարգավիճակ", callback_data="admin_status")],
        [InlineKeyboardButton("💾 Database", callback_data="admin_database")],
        [InlineKeyboardButton("👥 Օգտատերեր", callback_data="admin_users")],
        [InlineKeyboardButton("🟢 Ակտիվներ", callback_data="admin_active")],
        [InlineKeyboardButton("🆕 Նոր օգտատերեր", callback_data="admin_new")],
        [InlineKeyboardButton("❤️ Likes", callback_data="admin_likes"),
         InlineKeyboardButton("⭐ Super Likes", callback_data="admin_super")],
        [InlineKeyboardButton("❌ Pass", callback_data="admin_passes"),
         InlineKeyboardButton("💞 Matches", callback_data="admin_matches")],
        [InlineKeyboardButton("💬 Հաղորդագրություններ", callback_data="admin_messages")],
        [InlineKeyboardButton("👁️ Դիտումներ", callback_data="admin_views")],
        [InlineKeyboardButton("🚨 Reports", callback_data="admin_reports")],
        [InlineKeyboardButton("📋 Գործողությունների պատմություն", callback_data="admin_activity")],
        [InlineKeyboardButton("🔄 Թարմացնել", callback_data="admin_panel")],
    ])


def back_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Admin մենյու", callback_data="admin_panel")]
    ])


def database_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Ստեղծել Backup հիմա", callback_data="db_backup")],
        [InlineKeyboardButton("🛡️ Ստուգել Database-ը", callback_data="db_check")],
        [InlineKeyboardButton("📦 Backup-ների ցուցակ", callback_data="db_backups")],
        [InlineKeyboardButton("🔄 Թարմացնել", callback_data="admin_database")],
        [InlineKeyboardButton("◀️ Admin մենյու", callback_data="admin_panel")],
    ])


async def show_database_menu(query):
    ok, integrity = database_integrity_check()
    backups = list_backups(10)
    db_exists = db_path().exists()
    db_size = db_path().stat().st_size if db_exists else 0

    status = "🟢 Անվտանգ է" if ok else "🔴 Խնդիր կա"
    size_mb = db_size / (1024 * 1024)

    text = (
        "💾 <b>TOGETHR — DATABASE</b>\n\n"
        f"📁 Ֆայլ՝ <code>{html.escape(str(db_path()))}</code>\n"
        f"📦 Չափ՝ <b>{size_mb:.2f} MB</b>\n"
        f"🛡️ Integrity՝ <b>{status}</b>\n"
        f"🔎 Ստուգման արդյունք՝ <code>{html.escape(str(integrity))}</code>\n"
        f"💾 Backup-ներ՝ <b>{len(list_backups(100000))}</b>\n"
        f"🔁 Ավտոմատ Backup՝ յուրաքանչյուր <b>{max(BACKUP_INTERVAL_HOURS, 1)} ժամ</b>\n"
        f"🗃️ Պահպանվում է առավելագույնը՝ <b>{max(BACKUP_KEEP, 1)}</b> backup\n\n"
        "<b>Պաշտպանություն</b>\n"
        "• SQLite WAL mode\n"
        "• synchronous=FULL\n"
        "• Integrity check\n"
        "• Online SQLite backup\n"
        "• Հին backup-ների ավտոմատ մաքրում"
    )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=database_keyboard(),
    )


async def database_backup_callback(query, application):
    await query.edit_message_text(
        "⏳ <b>Backup-ը ստեղծվում է...</b>",
        parse_mode="HTML",
    )
    try:
        path, message = create_database_backup("manual")
        if path:
            await admin_notify(
                application,
                "💾 <b>Database Backup</b>\n\n"
                f"✅ Backup ստեղծվեց։\n"
                f"📁 {html.escape(str(path))}"
            )
            text = (
                "💾 <b>Backup-ը հաջողությամբ ստեղծվեց</b>\n\n"
                f"📁 <code>{html.escape(str(path))}</code>\n"
                "🛡️ Integrity ստուգումը՝ OK"
            )
        else:
            text = "🔴 <b>Backup-ը չստեղծվեց</b>\n\n" + html.escape(message)
    except Exception as exc:
        logger.exception("Manual backup failed")
        text = "🔴 <b>Backup-ի սխալ</b>\n\n" + html.escape(str(exc))

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=database_keyboard(),
    )


async def database_check_callback(query):
    ok, result = database_integrity_check()
    if ok:
        text = (
            "🛡️ <b>Database ստուգում</b>\n\n"
            "🟢 Database-ը ամբողջական է և վնասվածության նշան չկա։\n\n"
            f"Արդյունք՝ <code>{html.escape(str(result))}</code>"
        )
    else:
        text = (
            "🛡️ <b>Database ստուգում</b>\n\n"
            "🔴 <b>Խնդիր է հայտնաբերվել։</b>\n\n"
            f"Արդյունք՝ <code>{html.escape(str(result))}</code>"
        )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=database_keyboard(),
    )


async def database_backups_callback(query):
    backups = list_backups(15)

    if not backups:
        text = "📦 <b>Backup-ներ</b>\n\nԴեռ backup չկա։"
    else:
        lines = ["📦 <b>Վերջին backup-ները</b>\n"]
        for i, item in enumerate(backups, 1):
            try:
                size = item.stat().st_size / (1024 * 1024)
                when = datetime.fromtimestamp(
                    item.stat().st_mtime, timezone.utc
                ).astimezone().strftime("%d.%m.%Y %H:%M")
                lines.append(
                    f"{i}. 💾 <code>{html.escape(item.name)}</code>\n"
                    f"   🕐 {when} | {size:.2f} MB"
                )
            except OSError:
                continue
        text = "\n".join(lines)

    await query.edit_message_text(
        text[:3900],
        parse_mode="HTML",
        reply_markup=database_keyboard(),
    )


def stats():
    conn = db()
    now = datetime.now(timezone.utc)
    d1 = (now - timedelta(days=1)).isoformat()
    d7 = (now - timedelta(days=7)).isoformat()
    d30 = (now - timedelta(days=30)).isoformat()
    today = now.date().isoformat()

    def one(sql, args=()):
        return conn.execute(sql, args).fetchone()[0]

    result = {
        "users": one("SELECT COUNT(*) FROM users"),
        "complete": one("SELECT COUNT(*) FROM users WHERE name IS NOT NULL AND age IS NOT NULL AND city IS NOT NULL AND gender IS NOT NULL AND looking_for IS NOT NULL AND about IS NOT NULL AND photo_file_id IS NOT NULL"),
        "active24": one("SELECT COUNT(*) FROM users WHERE last_active>=?", (d1,)),
        "active7": one("SELECT COUNT(*) FROM users WHERE last_active>=?", (d7,)),
        "active30": one("SELECT COUNT(*) FROM users WHERE last_active>=?", (d30,)),
        "banned": one("SELECT COUNT(*) FROM users WHERE banned=1"),
        "likes": one("SELECT COUNT(*) FROM swipes WHERE action='like'"),
        "super": one("SELECT COUNT(*) FROM swipes WHERE action='super'"),
        "passes": one("SELECT COUNT(*) FROM swipes WHERE action='pass'"),
        "matches": one("SELECT COUNT(*) FROM matches"),
        "messages": one("SELECT COUNT(*) FROM messages"),
        "reports": one("SELECT COUNT(*) FROM reports WHERE status='open'"),
        "views": one("SELECT COUNT(*) FROM activity_logs WHERE action='Պրոֆիլ դիտվեց'"),
        "today_new": one("SELECT COUNT(*) FROM users WHERE substr(created_at,1,10)=?", (today,)),
        "today_actions": one("SELECT COUNT(*) FROM activity_logs WHERE substr(created_at,1,10)=?", (today,)),
        "today_messages": one("SELECT COUNT(*) FROM messages WHERE substr(created_at,1,10)=?", (today,)),
        "today_matches": one("SELECT COUNT(*) FROM matches WHERE substr(created_at,1,10)=?", (today,)),
    }
    conn.close()
    return result


async def show_admin_status(query):
    s = stats()
    text = (
        "📊 <b>TOGETHR — ԲՈՏԻ ԿԱՐԳԱՎԻՃԱԿ</b>\n\n"
        "🟢 <b>Կարգավիճակ՝ ԱՇԽԱՏՈՒՄ Է</b>\n"
        f"⏱️ Աշխատանքի ժամանակ՝ <b>{uptime_text()}</b>\n"
        f"🕐 Վերջին թարմացում՝ {fmt_time(now_iso())}\n\n"
        "👥 <b>Օգտատերեր</b>\n"
        f"• Ընդհանուր՝ {s['users']}\n"
        f"• Լրացված պրոֆիլներ՝ {s['complete']}\n"
        f"• Ակտիվ 24ժ՝ {s['active24']}\n"
        f"• Ակտիվ 7 օր՝ {s['active7']}\n"
        f"• Ակտիվ 30 օր՝ {s['active30']}\n"
        f"• Արգելափակված՝ {s['banned']}\n\n"
        "💘 <b>Գործողություններ</b>\n"
        f"• ❤️ Likes՝ {s['likes']}\n"
        f"• ⭐ Super Likes՝ {s['super']}\n"
        f"• ❌ Pass՝ {s['passes']}\n"
        f"• 💞 Matches՝ {s['matches']}\n"
        f"• 💬 Հաղորդագրություններ՝ {s['messages']}\n"
        f"• 👁️ Պրոֆիլի դիտումներ՝ {s['views']}\n"
        f"• 🚨 Բաց Reports՝ {s['reports']}\n\n"
        "📅 <b>Այսօր</b>\n"
        f"• 🆕 Նոր օգտատերեր՝ {s['today_new']}\n"
        f"• ⚡ Գործողություններ՝ {s['today_actions']}\n"
        f"• 💬 Հաղորդագրություններ՝ {s['today_messages']}\n"
        f"• 💞 Matches՝ {s['today_matches']}"
    )
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Թարմացնել", callback_data="admin_status")],
            [InlineKeyboardButton("📋 Վերջին գործողություններ", callback_data="admin_activity")],
            [InlineKeyboardButton("◀️ Admin մենյու", callback_data="admin_panel")]
        ])
    )


async def admin_activity(query):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM activity_logs ORDER BY id DESC LIMIT 25"
    ).fetchall()
    conn.close()

    if not rows:
        text = "📋 <b>Գործողություններ</b>\n\nՏվյալներ դեռ չկան։"
    else:
        lines = ["📋 <b>Վերջին 25 գործողությունները</b>\n"]
        for r in rows:
            who = label(r["user_id"]) if r["user_id"] else "Համակարգ"
            details = f" — {r['details']}" if r["details"] else ""
            lines.append(
                f"• {fmt_time(r['created_at'])}\n"
                f"👤 {html.escape(who)}\n"
                f"⚡ {html.escape(r['action'])}{html.escape(details)}\n"
            )
        text = "\n".join(lines)

    await query.edit_message_text(
        text[:3900],
        parse_mode="HTML",
        reply_markup=back_admin()
    )


async def admin_list(query, mode):
    conn = db()

    if mode == "users":
        rows = conn.execute(
            "SELECT * FROM users ORDER BY id DESC LIMIT 30"
        ).fetchall()
        title = "👥 Վերջին 30 օգտատերերը"
    elif mode == "active":
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        rows = conn.execute(
            "SELECT * FROM users WHERE last_active>=? ORDER BY last_active DESC LIMIT 30",
            (since,)
        ).fetchall()
        title = "🟢 Ակտիվ օգտատերեր՝ 24ժ"
    elif mode == "new":
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        title = "🆕 Վերջին գրանցումները"
    else:
        rows = []

    conn.close()

    if not rows:
        text = f"<b>{title}</b>\n\nՏվյալներ չկան։"
    else:
        lines = [f"<b>{title}</b>\n"]
        for r in rows:
            status = "🚫" if r["banned"] else "🟢"
            lines.append(
                f"{status} <b>{html.escape(r['name'] or 'Անուն չկա')}</b>\n"
                f"🆔 {r['id']} | {html.escape(r['username'] or 'username չկա')}\n"
                f"🕐 {fmt_time(r['last_active'])}\n"
            )
        text = "\n".join(lines)

    await query.edit_message_text(
        text[:3900],
        parse_mode="HTML",
        reply_markup=back_admin()
    )


async def admin_simple_count(query, title, sql):
    conn = db()
    count = conn.execute(sql).fetchone()[0]
    conn.close()
    await query.edit_message_text(
        f"<b>{title}</b>\n\n📊 Ընդհանուր՝ <b>{count}</b>",
        parse_mode="HTML",
        reply_markup=back_admin()
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_user(user)

    if is_banned(user.id):
        await update.message.reply_text("🚫 Ձեր մուտքը բոտ արգելափակված է։")
        return

    row = get_user(user.id)

    if not profile_complete(row):
        context.user_data["step"] = "name"
        await update.message.reply_text(
            "👋 Բարի գալուստ <b>Togethr</b>։\n\n"
            "Սկսենք պրոֆիլից։\n\nԳրեք ձեր անունը։",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    await home(update, context)


async def home(update, context):
    context.user_data.pop("step", None)
    context.user_data.pop("chat_match_id", None)

    keyboard = ReplyKeyboardMarkup(
        [
            ["👤 Իմ պրոֆիլը", "🔎 Գտնել մարդկանց"],
            ["❤️ Իմ Match-երը", "✏️ Խմբագրել պրոֆիլը"],
        ],
        resize_keyboard=True
    )

    target = update.message
    await target.reply_text(
        "🏠 <b>Գլխավոր մենյու</b>\n\nԸնտրեք գործողությունը։",
        parse_mode="HTML",
        reply_markup=keyboard
    )


def is_admin(user_id):
    return ADMIN_ID and user_id == ADMIN_ID


async def admin_command(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "⚙️ <b>Admin Panel</b>\n\nԸնտրեք բաժինը։",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "admin_panel":
        if is_admin(query.from_user.id):
            await query.edit_message_text(
                "⚙️ <b>Admin Panel</b>\n\nԸնտրեք բաժինը։",
                parse_mode="HTML",
                reply_markup=admin_keyboard()
            )
        return

    if data == "admin_status":
        if is_admin(query.from_user.id):
            await show_admin_status(query)
        return

    if data == "admin_database":
        if is_admin(query.from_user.id):
            await show_database_menu(query)
        return

    if data == "db_backup":
        if is_admin(query.from_user.id):
            await database_backup_callback(query, context.application)
        return

    if data == "db_check":
        if is_admin(query.from_user.id):
            await database_check_callback(query)
        return

    if data == "db_backups":
        if is_admin(query.from_user.id):
            await database_backups_callback(query)
        return

    if data == "admin_activity":
        if is_admin(query.from_user.id):
            await admin_activity(query)
        return

    if data == "admin_users":
        if is_admin(query.from_user.id):
            await admin_list(query, "users")
        return

    if data == "admin_active":
        if is_admin(query.from_user.id):
            await admin_list(query, "active")
        return

    if data == "admin_new":
        if is_admin(query.from_user.id):
            await admin_list(query, "new")
        return

    if data == "admin_likes":
        if is_admin(query.from_user.id):
            await admin_simple_count(query, "❤️ Likes", "SELECT COUNT(*) FROM swipes WHERE action='like'")
        return

    if data == "admin_super":
        if is_admin(query.from_user.id):
            await admin_simple_count(query, "⭐ Super Likes", "SELECT COUNT(*) FROM swipes WHERE action='super'")
        return

    if data == "admin_passes":
        if is_admin(query.from_user.id):
            await admin_simple_count(query, "❌ Pass", "SELECT COUNT(*) FROM swipes WHERE action='pass'")
        return

    if data == "admin_matches":
        if is_admin(query.from_user.id):
            await admin_simple_count(query, "💞 Matches", "SELECT COUNT(*) FROM matches")
        return

    if data == "admin_messages":
        if is_admin(query.from_user.id):
            await admin_simple_count(query, "💬 Հաղորդագրություններ", "SELECT COUNT(*) FROM messages")
        return

    if data == "admin_views":
        if is_admin(query.from_user.id):
            await admin_simple_count(query, "👁️ Պրոֆիլի դիտումներ", "SELECT COUNT(*) FROM activity_logs WHERE action='Պրոֆիլ դիտվեց'")
        return

    if data == "admin_reports":
        if is_admin(query.from_user.id):
            await admin_simple_count(query, "🚨 Բաց Reports", "SELECT COUNT(*) FROM reports WHERE status='open'")
        return

    if data == "home":
        await query.message.delete()
        await home(update, context)
        return


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_user(user)
    update_user(user.id, username=user.username, last_active=now_iso())

    if is_banned(user.id):
        await update.message.reply_text("🚫 Ձեր մուտքը բոտ արգելափակված է։")
        return

    text = update.message.text
    step = context.user_data.get("step")

    if text == "🏠 Գլխավոր":
        await home(update, context)
        return

    if text == "👤 Իմ պրոֆիլը":
        row = get_user(user.id)
        await update.message.reply_text(
            f"👤 <b>{html.escape(row['name'] or '—')}</b>\n"
            f"🎂 Տարիք՝ {row['age'] or '—'}\n"
            f"📍 Քաղաք՝ {html.escape(row['city'] or '—')}\n"
            f"⚥ Սեռ՝ {html.escape(row['gender'] or '—')}\n"
            f"🔎 Փնտրում է՝ {html.escape(row['looking_for'] or '—')}\n\n"
            f"📝 {html.escape(row['about'] or '—')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Խմբագրել", callback_data="edit")],
                [InlineKeyboardButton("🏠 Գլխավոր", callback_data="home")]
            ])
        )
        return

    if step == "name":
        update_user(user.id, name=text)
        context.user_data["step"] = "age"
        await update.message.reply_text("🎂 Գրեք ձեր տարիքը (18–99)։")
        return

    if step == "age":
        try:
            age = int(text)
        except ValueError:
            await update.message.reply_text("❗ Տարիքը գրեք թվով, օրինակ՝ 25։")
            return
        if not 18 <= age <= 99:
            await update.message.reply_text("❗ Տարիքը պետք է լինի 18–99։")
            return
        update_user(user.id, age=age)
        context.user_data["step"] = "city"
        await update.message.reply_text("📍 Գրեք ձեր քաղաքը։")
        return

    if step == "city":
        update_user(user.id, city=text)
        context.user_data["step"] = None
        await update.message.reply_text(
            "⚥ Ընտրեք ձեր սեռը։",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👨 Տղամարդ", callback_data="gender_male")],
                [InlineKeyboardButton("👩 Կին", callback_data="gender_female")],
            ])
        )
        return

    await update.message.reply_text(
        "Խնդրում եմ օգտագործել մենյուի կոճակները։"
    )


async def callback_profile(update, context):
    pass


async def photo_router(update, context):
    user = update.effective_user
    create_user(user)
    photo = update.message.photo[-1]
    update_user(user.id, photo_file_id=photo.file_id)
    context.user_data["step"] = None
    await update.message.reply_text(
        "✅ Պրոֆիլը պահպանվեց։\n\n🏠 Օգտագործեք գլխավոր մենյուի կոճակները։",
        reply_markup=ReplyKeyboardMarkup(
            [["👤 Իմ պրոֆիլը", "🔎 Գտնել մարդկանց"],
             ["❤️ Իմ Match-երը", "✏️ Խմբագրել պրոֆիլը"]],
            resize_keyboard=True
        )
    )


async def error_handler(update, context):
    logger.exception("Unhandled exception", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")

    init_db()

    # Never delete/replace the existing database during a code deployment.
    # Make a verified backup before the bot starts serving users.
    try:
        path, message = create_database_backup("startup")
        logger.info("Startup database backup: %s | %s", path or "not-created", message)
    except Exception:
        logger.exception("Startup database backup failed")

    application = Application.builder().token(BOT_TOKEN).build()

    backup_scheduler(application)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.PHOTO, photo_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    application.add_error_handler(error_handler)

    logger.info("TOGETHR started")
    application.run_polling()


if __name__ == "__main__":
    main()

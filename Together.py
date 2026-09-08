import os
import sqlite3
import logging
from datetime import datetime, timedelta
from html import escape

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_FILE = os.getenv("DB_FILE", "/data/together.db")
INACTIVITY_SECONDS = 180
ACTIVE_DAYS = 7

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("Together")

NAME, AGE, CITY, GENDER, LOOKING_FOR, ABOUT, PHOTO = range(7)


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


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '', name TEXT, age INTEGER, city TEXT,
            gender TEXT, looking_for TEXT, about TEXT, photo_file_id TEXT,
            banned INTEGER DEFAULT 0, created_at TEXT NOT NULL, last_active TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS swipes (
            from_user INTEGER NOT NULL, to_user INTEGER NOT NULL,
            action TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(from_user,to_user)
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1 INTEGER NOT NULL, user2 INTEGER NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(user1,user2)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
            text TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter INTEGER NOT NULL, reported INTEGER NOT NULL,
            reason TEXT NOT NULL, status TEXT DEFAULT 'new', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS blocks (
            blocker INTEGER NOT NULL, blocked INTEGER NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(blocker,blocked)
        );
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            action TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO bot_settings(key,value) VALUES('activity_notifications','1');
        CREATE INDEX IF NOT EXISTS idx_users_active ON users(last_active,banned);
        CREATE INDEX IF NOT EXISTS idx_swipes_from ON swipes(from_user);
        CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
        CREATE INDEX IF NOT EXISTS idx_messages_match ON messages(match_id,created_at);
        """)


def ensure_user(tg_user):
    t = now()
    with db() as conn:
        conn.execute("""
            INSERT INTO users(id,username,created_at,last_active)
            VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET username=excluded.username,last_active=excluded.last_active
        """, (tg_user.id, tg_user.username or "", t, t))


def touch(user_id):
    with db() as conn:
        conn.execute("UPDATE users SET last_active=? WHERE id=?", (now(), user_id))


def get_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def is_banned(user_id):
    u = get_user(user_id)
    return bool(u and u["banned"])


def update_user(user_id, **fields):
    allowed = {"username","name","age","city","gender","looking_for","about","photo_file_id","banned"}
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return
    fields["last_active"] = now()
    sql = ",".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE users SET {sql} WHERE id=?", [*fields.values(), user_id])


def log_activity(user_id, action):
    with db() as conn:
        conn.execute("INSERT INTO activity_logs(user_id,action,created_at) VALUES(?,?,?)", (user_id,action,now()))


def profile_complete(user_id):
    u = get_user(user_id)
    return bool(u and all([u["name"], u["age"], u["city"], u["gender"], u["looking_for"], u["about"], u["photo_file_id"]]))


def activity_notifications_enabled():
    with db() as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key='activity_notifications'").fetchone()
    return bool(row and row["value"] == "1")


def set_activity_notifications(enabled):
    with db() as conn:
        conn.execute("""INSERT INTO bot_settings(key,value) VALUES('activity_notifications',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""", ("1" if enabled else "0",))


async def notify_admin(context, text):
    if ADMIN_ID and activity_notifications_enabled():
        try:
            await context.bot.send_message(ADMIN_ID, text)
        except Exception:
            log.exception("Admin notification failed")


def main_keyboard(user_id):
    rows = [["👤 Իմ պրոֆիլը", "🔎 Գտնել մարդկանց"], ["❤️ Իմ Match-երը", "✏️ Խմբագրել պրոֆիլը"], ["⚙️ Կարգավորումներ"]]
    if user_id == ADMIN_ID:
        rows.append(["🛡️ Admin մենյու"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup([["⬅️ Չեղարկել"]], resize_keyboard=True)


def choice_keyboard(values):
    return ReplyKeyboardMarkup([[v] for v in values], resize_keyboard=True, one_time_keyboard=True)


def settings_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Ջնջել իմ պրոֆիլը", callback_data="delete_profile")], [InlineKeyboardButton("⬅️ Գլխավոր մենյու", callback_data="home")]])


def admin_keyboard():
    status = "🟢 Միացված" if activity_notifications_enabled() else "🔴 Անջատված"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Վիճակագրություն", callback_data="admin:stats")],
        [InlineKeyboardButton("👥 Օգտատերեր", callback_data="admin:users")],
        [InlineKeyboardButton("🚨 Հաղորդումներ", callback_data="admin:reports")],
        [InlineKeyboardButton(f"🔔 Ակտիվության հաղորդագրություններ՝ {status}", callback_data="admin:activity_toggle")],
        [InlineKeyboardButton("⬅️ Գլխավոր մենյու", callback_data="home")]
    ])


def profile_text(u):
    return (
        f"👤 <b>{escape(str(u['name']))}</b>\n"
        f"🎂 {u['age']} տարեկան\n"
        f"📍 {escape(str(u['city']))}\n"
        f"⚧️ {escape(str(u['gender']))}\n"
        f"❤️ Փնտրում է՝ {escape(str(u['looking_for']))}\n\n"
        f"💬 {escape(str(u['about']))}"
    )


def compatible(a,b):
    return bool(a and b and a["looking_for"] == b["gender"] and b["looking_for"] == a["gender"])


def blocked_between(a,b):
    with db() as conn:
        return conn.execute("""SELECT 1 FROM blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)""", (a,b,b,a)).fetchone() is not None


def next_candidate(user_id):
    me = get_user(user_id)
    if not me:
        return None
    cutoff = (datetime.utcnow()-timedelta(days=ACTIVE_DAYS)).isoformat(timespec="seconds")
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM users WHERE id!=? AND banned=0 AND last_active>=?
            AND name IS NOT NULL AND age IS NOT NULL AND city IS NOT NULL
            AND gender IS NOT NULL AND looking_for IS NOT NULL AND about IS NOT NULL
            AND photo_file_id IS NOT NULL
            AND NOT EXISTS(SELECT 1 FROM swipes s WHERE s.from_user=? AND s.to_user=users.id)
            ORDER BY RANDOM() LIMIT 100
        """, (user_id,cutoff,user_id)).fetchall()
    for c in rows:
        if compatible(me,c) and not blocked_between(user_id,c["id"]):
            return c
    return None


def candidate_keyboard(cid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ Հավանել", callback_data=f"like:{cid}"), InlineKeyboardButton("🔥 Super Like", callback_data=f"super:{cid}")],
        [InlineKeyboardButton("👎 Հաջորդը", callback_data=f"pass:{cid}")],
        [InlineKeyboardButton("🚨 Հաղորդել", callback_data=f"report_menu:{cid}"), InlineKeyboardButton("🚫 Արգելափակել", callback_data=f"block:{cid}")]
    ])


async def send_candidate(message, user_id):
    candidate = next_candidate(user_id)
    if not candidate:
        await message.reply_text("🔎 Այս պահին համապատասխան նոր պրոֆիլ չգտնվեց։\n\nՓորձեք մի փոքր ուշ։")
        return False
    text = profile_text(candidate)
    kb = candidate_keyboard(candidate["id"])
    if candidate["photo_file_id"]:
        await message.reply_photo(candidate["photo_file_id"], caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return True


async def start(update, context):
    user = update.effective_user
    ensure_user(user)
    touch(user.id)
    if is_banned(user.id):
        await update.message.reply_text("🚫 Ձեր պրոֆիլը արգելափակված է։")
        return ConversationHandler.END
    context.user_data.clear()
    log_activity(user.id,"start")
    if profile_complete(user.id):
        await home(update,context)
        return ConversationHandler.END
    await update.message.reply_text("❤️ Բարի գալուստ Together։\n\nԱյստեղ կարող եք ծանոթանալ նոր մարդկանց հետ։\nՍկսելու համար լրացրեք ձեր պրոֆիլը։", reply_markup=cancel_keyboard())
    await update.message.reply_text("Ինչպե՞ս է ձեր անունը։")
    return NAME


async def home(update, context):
    uid = update.effective_user.id
    touch(uid)
    context.user_data["mode"]="home"
    text="❤️ <b>Together</b>\n\nԸնտրեք գործողությունը՝"
    if update.callback_query:
        q=update.callback_query
        await q.message.reply_text(text,parse_mode="HTML",reply_markup=main_keyboard(uid))
    else:
        await update.message.reply_text(text,parse_mode="HTML",reply_markup=main_keyboard(uid))


async def start_profile(update,context):
    context.user_data.clear(); context.user_data["editing"]=False
    await update.message.reply_text("✏️ Սկսենք պրոֆիլի լրացումը։\n\nԻնչպե՞ս է ձեր անունը։",reply_markup=cancel_keyboard())
    return NAME


async def edit_profile(update,context):
    context.user_data.clear(); context.user_data["editing"]=True
    await update.message.reply_text("✏️ Փոխենք ձեր պրոֆիլը։\n\nԳրեք ձեր անունը։",reply_markup=cancel_keyboard())
    return NAME


async def name_step(update,context):
    if update.message.text.strip()=="⬅️ Չեղարկել": return await cancel(update,context)
    v=update.message.text.strip()
    if not 2<=len(v)<=40:
        await update.message.reply_text("❌ Անունը պետք է լինի 2–40 նիշ։"); return NAME
    context.user_data["name"]=v
    await update.message.reply_text("🎂 Քանի՞ տարեկան եք։",reply_markup=cancel_keyboard()); return AGE


async def age_step(update,context):
    if update.message.text.strip()=="⬅️ Չեղարկել": return await cancel(update,context)
    try: age=int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Տարիքը գրեք թվով։ Օրինակ՝ 25"); return AGE
    if not 18<=age<=99:
        await update.message.reply_text("❌ Տարիքը պետք է լինի 18–99։"); return AGE
    context.user_data["age"]=age
    await update.message.reply_text("📍 Ո՞ր քաղաքում եք ապրում։",reply_markup=cancel_keyboard()); return CITY


async def city_step(update,context):
    if update.message.text.strip()=="⬅️ Չեղարկել": return await cancel(update,context)
    v=update.message.text.strip()
    if not 2<=len(v)<=50:
        await update.message.reply_text("❌ Գրեք քաղաքի ճիշտ անվանումը։"); return CITY
    context.user_data["city"]=v
    await update.message.reply_text("⚧️ Ընտրեք ձեր սեռը։",reply_markup=choice_keyboard(["👨 Տղամարդ","👩 Կին"])); return GENDER


async def gender_step(update,context):
    mapping={"👨 Տղամարդ":"Տղամարդ","👩 Կին":"Կին"}; v=update.message.text.strip()
    if v not in mapping:
        await update.message.reply_text("Խնդրում եմ ընտրեք տարբերակներից մեկը։",reply_markup=choice_keyboard(list(mapping))); return GENDER
    context.user_data["gender"]=mapping[v]
    await update.message.reply_text("❤️ Ո՞ւմ հետ եք ցանկանում ծանոթանալ։",reply_markup=choice_keyboard(["👨 Տղամարդ","👩 Կին"])); return LOOKING_FOR


async def looking_step(update,context):
    mapping={"👨 Տղամարդ":"Տղամարդ","👩 Կին":"Կին"}; v=update.message.text.strip()
    if v not in mapping:
        await update.message.reply_text("Խնդրում եմ ընտրեք տարբերակներից մեկը։",reply_markup=choice_keyboard(list(mapping))); return LOOKING_FOR
    context.user_data["looking_for"]=mapping[v]
    await update.message.reply_text("💬 Մի փոքր պատմեք ձեր մասին։\n\nՕրինակ՝ հետաքրքրություններ, զբաղմունք, ինչ եք փնտրում։",reply_markup=cancel_keyboard()); return ABOUT


async def about_step(update,context):
    if update.message.text.strip()=="⬅️ Չեղարկել": return await cancel(update,context)
    v=update.message.text.strip()
    if not 5<=len(v)<=500:
        await update.message.reply_text("❌ Գրեք 5–500 նիշի սահմաններում։"); return ABOUT
    context.user_data["about"]=v
    await update.message.reply_text("📸 Ուղարկեք ձեր լուսանկարը։",reply_markup=cancel_keyboard()); return PHOTO


async def photo_step(update,context):
    if update.message.text and update.message.text.strip()=="⬅️ Չեղարկել": return await cancel(update,context)
    if not update.message.photo:
        await update.message.reply_text("❌ Խնդրում եմ ուղարկեք լուսանկար։"); return PHOTO
    uid=update.effective_user.id
    update_user(uid,name=context.user_data.get("name"),age=context.user_data.get("age"),city=context.user_data.get("city"),gender=context.user_data.get("gender"),looking_for=context.user_data.get("looking_for"),about=context.user_data.get("about"),photo_file_id=update.message.photo[-1].file_id,username=update.effective_user.username or "")
    context.user_data.clear(); log_activity(uid,"profile_saved")
    await notify_admin(context,f"👤 Նոր/թարմացված պրոֆիլ՝ {uid}")
    await update.message.reply_text("✅ Ձեր պրոֆիլը պատրաստ է։\n\nԱյժմ կարող եք գտնել մարդկանց և ծանոթանալ։",reply_markup=main_keyboard(uid))
    return ConversationHandler.END


async def show_profile(update,context,user_id=None):
    uid=user_id or update.effective_user.id; u=get_user(uid)
    if not u: return
    text=profile_text(u)
    if update.callback_query:
        q=update.callback_query; await q.answer()
        if u["photo_file_id"]: await q.message.reply_photo(u["photo_file_id"],caption=text,parse_mode="HTML")
        else: await q.message.reply_text(text,parse_mode="HTML")
    else:
        if u["photo_file_id"]: await update.message.reply_photo(u["photo_file_id"],caption=text,parse_mode="HTML")
        else: await update.message.reply_text(text,parse_mode="HTML")


async def discover(update,context):
    uid=update.effective_user.id
    if not profile_complete(uid):
        await update.message.reply_text("❗ Նախ լրացրեք ձեր պրոֆիլը։",reply_markup=main_keyboard(uid)); return
    touch(uid); await send_candidate(update.message,uid)


async def swipe(update,context):
    q=update.callback_query; await q.answer(); uid=q.from_user.id
    try: action,target=q.data.split(":",1); target=int(target)
    except (ValueError,AttributeError): return
    if uid==target or is_banned(uid) or is_banned(target): return
    target_user=get_user(target); me=get_user(uid)
    if not target_user or not me or not compatible(me,target_user) or blocked_between(uid,target):
        await q.message.reply_text("❌ Այս պրոֆիլն այլևս հասանելի չէ։"); return
    action_db={"like":"like","super":"super","pass":"pass"}[action]
    with db() as conn:
        conn.execute("""INSERT INTO swipes(from_user,to_user,action,created_at) VALUES(?,?,?,?)
        ON CONFLICT(from_user,to_user) DO UPDATE SET action=excluded.action,created_at=excluded.created_at""",(uid,target,action_db,now()))
        mutual=conn.execute("SELECT 1 FROM swipes WHERE from_user=? AND to_user=? AND action IN('like','super')",(target,uid)).fetchone()
        match=None
        if mutual:
            u1,u2=sorted((uid,target))
            conn.execute("INSERT OR IGNORE INTO matches(user1,user2,created_at) VALUES(?,?,?)",(u1,u2,now()))
            match=conn.execute("SELECT id FROM matches WHERE user1=? AND user2=?",(u1,u2)).fetchone()
    log_activity(uid,action_db)
    if mutual and match:
        await q.message.edit_reply_markup(reply_markup=None)
        await q.message.reply_text("🎉 <b>Match!</b>\n\nԴուք երկուսդ էլ հավանել եք միմյանց։ ❤️",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Բացել չատը",callback_data=f"chat:{match['id']}")],[InlineKeyboardButton("🔎 Գտնել հաջորդին",callback_data="discover_next")]]))
        try: await context.bot.send_message(target,"🎉 Դուք նոր Match ունեք։ ❤️\nԲացեք Together-ը՝ զրույցը սկսելու համար։")
        except Exception: pass
    else:
        if action_db in ("like","super"):
            try: await context.bot.send_message(target,"❤️ Ինչ-որ մեկը հավանել է ձեր պրոֆիլը։ Եթե փոխադարձ լինի, կունենաք Match։")
            except Exception: pass
        await q.message.edit_reply_markup(reply_markup=None)
        await q.message.reply_text("➡️ Պրոֆիլը պահպանվեց։",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Հաջորդը",callback_data="discover_next")],[InlineKeyboardButton("🏠 Գլխավոր մենյու",callback_data="home")]]))


def get_matches(uid):
    with db() as conn:
        return conn.execute("SELECT *,CASE WHEN user1=? THEN user2 ELSE user1 END other_id FROM matches WHERE user1=? OR user2=? ORDER BY created_at DESC",(uid,uid,uid)).fetchall()


def find_match(mid,uid):
    with db() as conn:
        return conn.execute("SELECT * FROM matches WHERE id=? AND(user1=? OR user2=?)",(mid,uid,uid)).fetchone()


async def show_matches(update,context):
    uid=update.effective_user.id; matches=get_matches(uid)
    buttons=[]
    for m in matches:
        other=get_user(m["other_id"])
        if other and not blocked_between(uid,other["id"]): buttons.append([InlineKeyboardButton(f"💬 {escape(other['name'] or 'Օգտատեր')}",callback_data=f"chat:{m['id']}")])
    if not buttons:
        await update.message.reply_text("❤️ Դեռ ակտիվ Match չունեք։\n\nԳնացեք «🔎 Գտնել մարդկանց» բաժին։"); return
    await update.message.reply_text("❤️ <b>Ձեր Match-երը</b>\n\nԸնտրեք զրույցը։",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(buttons))


async def open_chat(update,context):
    q=update.callback_query; await q.answer(); uid=q.from_user.id
    try: mid=int(q.data.split(":",1)[1])
    except ValueError: return
    match=find_match(mid,uid)
    if not match: await q.message.reply_text("❌ Զրույցը հասանելի չէ։"); return
    other_id=match["user2"] if match["user1"]==uid else match["user1"]; other=get_user(other_id)
    if not other or blocked_between(uid,other_id): await q.message.reply_text("🚫 Զրույցը հասանելի չէ։"); return
    context.user_data["chat_match_id"]=mid; context.user_data["chat_other_id"]=other_id; context.user_data["last_chat_activity"]=datetime.utcnow().timestamp()
    touch(uid)
    await q.message.reply_text(f"💬 Դուք զրուցում եք <b>{escape(other['name'])}</b>-ի հետ։\n\nԳրեք հաղորդագրություն։\nՉատը 3 րոպե անգործությունից ավտոմատ կփակվի։",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Արգելափակել",callback_data=f"block:{other_id}"),InlineKeyboardButton("🚨 Հաղորդել",callback_data=f"report_menu:{other_id}")],[InlineKeyboardButton("🏠 Գլխավոր մենյու",callback_data="home")]]))


async def send_chat_message(update,context):
    uid=update.effective_user.id; mid=context.user_data.get("chat_match_id"); other_id=context.user_data.get("chat_other_id")
    if not mid or not other_id: return False
    if blocked_between(uid,other_id):
        context.user_data.clear(); await update.message.reply_text("🚫 Զրույցը հասանելի չէ։",reply_markup=main_keyboard(uid)); return True
    text=update.message.text.strip()
    if not text: return True
    match=find_match(mid,uid)
    if not match: context.user_data.clear(); return True
    text=text[:2000]
    with db() as conn: conn.execute("INSERT INTO messages(match_id,sender_id,text,created_at) VALUES(?,?,?,?)",(mid,uid,text,now()))
    context.user_data["last_chat_activity"]=datetime.utcnow().timestamp(); touch(uid)
    try: await context.bot.send_message(other_id,f"💬 Նոր հաղորդագրություն՝\n\n{text}")
    except Exception: pass
    await update.message.reply_text("✅ Ուղարկվեց։")
    return True


async def inactivity_cleanup(context):
    now_ts=datetime.utcnow().timestamp()
    for uid,data in list(context.application.user_data.items()):
        last=data.get("last_chat_activity")
        if last and now_ts-last>=INACTIVITY_SECONDS:
            data.clear()
            try: await context.bot.send_message(uid,"⏱️ Չատը փակվեց 3 րոպե անգործությունից։\n\nՁեր Match-երը պահպանվել են։",reply_markup=main_keyboard(uid))
            except Exception: pass


async def report_menu(update,context):
    q=update.callback_query; await q.answer(); target=int(q.data.split(":",1)[1])
    await q.message.reply_text("🚨 Ընտրեք հաղորդման պատճառը։",reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Անպատշաճ բովանդակություն",callback_data=f"report:{target}:inappropriate")],
        [InlineKeyboardButton("👤 Կեղծ պրոֆիլ",callback_data=f"report:{target}:fake")],
        [InlineKeyboardButton("⚠️ Վիրավորանք / չարաշահում",callback_data=f"report:{target}:abuse")],
        [InlineKeyboardButton("📝 Այլ",callback_data=f"report:{target}:other")],
        [InlineKeyboardButton("🚫 Արգելափակել",callback_data=f"block:{target}")]]))


async def report_user(update,context):
    q=update.callback_query; await q.answer("Հաղորդումը ստացվեց։"); _,target,reason=q.data.split(":",2); target=int(target); uid=q.from_user.id
    if target==uid: return
    with db() as conn: conn.execute("INSERT INTO reports(reporter,reported,reason,created_at) VALUES(?,?,?,?)",(uid,target,reason,now()))
    log_activity(uid,"report")
    await notify_admin(context,f"🚨 Նոր հաղորդում\nReporter: {uid}\nReported: {target}\nՊատճառ: {reason}")
    await q.message.reply_text("✅ Հաղորդումը ուղարկվեց ադմինին։")


async def block_user(update,context):
    q=update.callback_query; target=int(q.data.split(":",1)[1]); uid=q.from_user.id
    if target==uid: await q.answer("Չեք կարող արգելափակել ինքներդ ձեզ։",show_alert=True); return
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO blocks(blocker,blocked,created_at) VALUES(?,?,?)",(uid,target,now()))
        conn.execute("DELETE FROM swipes WHERE(from_user=? AND to_user=?) OR(from_user=? AND to_user=?)",(uid,target,target,uid))
    context.user_data.clear(); log_activity(uid,"block"); await q.answer("Օգտատերը արգելափակվեց։")
    await q.message.reply_text("🚫 Օգտատերը արգելափակվեց։ Նրա պրոֆիլը այլևս չի ցուցադրվի ձեզ։",reply_markup=main_keyboard(uid))


async def settings(update,context):
    await update.message.reply_text("⚙️ <b>Կարգավորումներ</b>\n\nԿառավարեք ձեր պրոֆիլը և գաղտնիության գործողությունները։",parse_mode="HTML",reply_markup=settings_keyboard())


async def delete_confirm(update,context):
    q=update.callback_query; await q.answer()
    await q.message.reply_text("⚠️ <b>Պրոֆիլի մշտական ջնջում</b>\n\nՊրոֆիլը, Match-երը, Like-երը, հաղորդագրությունները և անձնական տվյալները կջնջվեն։ Գործողությունը հնարավոր չէ հետարկել։\n\nՇարունակե՞լ։",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Այո, ջնջել պրոֆիլը",callback_data="delete_yes")],[InlineKeyboardButton("⬅️ Չեղարկել",callback_data="home")]]))


async def delete_profile(update,context):
    q=update.callback_query; uid=q.from_user.id; await q.answer("Պրոֆիլը ջնջվում է…")
    with db() as conn:
        ids=[r["id"] for r in conn.execute("SELECT id FROM matches WHERE user1=? OR user2=?",(uid,uid)).fetchall()]
        if ids:
            ph=",".join("?"*len(ids)); conn.execute(f"DELETE FROM messages WHERE match_id IN({ph})",ids)
        conn.execute("DELETE FROM matches WHERE user1=? OR user2=?",(uid,uid)); conn.execute("DELETE FROM swipes WHERE from_user=? OR to_user=?",(uid,uid)); conn.execute("DELETE FROM blocks WHERE blocker=? OR blocked=?",(uid,uid)); conn.execute("DELETE FROM reports WHERE reporter=? OR reported=?",(uid,uid)); conn.execute("DELETE FROM activity_logs WHERE user_id=?",(uid,)); conn.execute("DELETE FROM users WHERE id=?",(uid,))
    context.user_data.clear(); await q.message.reply_text("🗑️ Ձեր Together պրոֆիլը ամբողջությամբ ջնջվեց։\n\nՎերադառնալու համար օգտագործեք /start。",reply_markup=ReplyKeyboardMarkup([["🚀 Սկսել նորից"]],resize_keyboard=True))


async def admin_menu(update,context):
    if update.effective_user.id!=ADMIN_ID: return
    await update.message.reply_text("🛡️ <b>Admin մենյու</b>",parse_mode="HTML",reply_markup=admin_keyboard())


async def admin_callback(update,context):
    q=update.callback_query
    if q.from_user.id!=ADMIN_ID: await q.answer("Մուտքն արգելված է։",show_alert=True); return
    await q.answer(); action=q.data.split(":",1)[1]
    if action=="activity_toggle":
        enabled=not activity_notifications_enabled(); set_activity_notifications(enabled); await q.message.edit_reply_markup(reply_markup=admin_keyboard()); return
    with db() as conn:
        if action=="stats":
            users=conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]; active=conn.execute("SELECT COUNT(*) c FROM users WHERE last_active>=? AND banned=0",((datetime.utcnow()-timedelta(days=7)).isoformat(timespec="seconds"),)).fetchone()["c"]; matches=conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"]; messages=conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]; reports=conn.execute("SELECT COUNT(*) c FROM reports WHERE status='new'").fetchone()["c"]
            await q.message.reply_text(f"📊 <b>Վիճակագրություն</b>\n\n👥 Օգտատերեր՝ {users}\n🟢 Ակտիվ՝ {active}\n❤️ Match-եր՝ {matches}\n💬 Հաղորդագրություններ՝ {messages}\n🚨 Նոր հաղորդումներ՝ {reports}",parse_mode="HTML")
        elif action=="users":
            rows=conn.execute("SELECT id,name,city,banned FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
            lines=["👥 <b>Վերջին օգտատերերը</b>\n"]+[f"{'🚫' if r['banned'] else '🟢'} {r['id']} — {escape(r['name'] or 'Անուն չկա')} — {escape(r['city'] or '-') }" for r in rows]
            await q.message.reply_text("\n".join(lines) if rows else "Օգտատերեր չկան։",parse_mode="HTML")
        elif action=="reports":
            rows=conn.execute("SELECT reporter,reported,reason,created_at FROM reports WHERE status='new' ORDER BY created_at DESC LIMIT 20").fetchall()
            lines=["🚨 <b>Հաղորդումներ</b>\n"]+[f"👤 {r['reporter']} → {r['reported']}\n📝 {escape(r['reason'])}\n🕒 {r['created_at']}" for r in rows]
            await q.message.reply_text("\n\n".join(lines) if rows else "🚨 Նոր հաղորդումներ չկան։",parse_mode="HTML")


async def cancel(update,context):
    uid=update.effective_user.id; context.user_data.clear(); await home(update,context); return ConversationHandler.END


async def text_router(update,context):
    uid=update.effective_user.id; ensure_user(update.effective_user)
    if is_banned(uid): await update.message.reply_text("🚫 Ձեր պրոֆիլը արգելափակված է։"); return
    touch(uid)
    if context.user_data.get("chat_match_id"):
        if await send_chat_message(update,context): return
    text=update.message.text.strip()
    if text in ("🚀 Սկսել նորից",): return await start(update,context)
    if text=="👤 Իմ պրոֆիլը": await show_profile(update,context)
    elif text=="🔎 Գտնել մարդկանց": await discover(update,context)
    elif text=="❤️ Իմ Match-երը": await show_matches(update,context)
    elif text=="✏️ Խմբագրել պրոֆիլը": await edit_profile(update,context)
    elif text=="⚙️ Կարգավորումներ": await settings(update,context)
    elif text=="🛡️ Admin մենյու" and uid==ADMIN_ID: await admin_menu(update,context)
    else: await update.message.reply_text("Խնդրում եմ ընտրեք գործողությունը կոճակներից։",reply_markup=main_keyboard(uid))


async def callback_router(update,context):
    data=update.callback_query.data or ""
    if data.startswith(("like:","super:","pass:")): await swipe(update,context)
    elif data=="discover_next":
        q=update.callback_query; await q.answer(); touch(q.from_user.id); await send_candidate(q.message,q.from_user.id)
    elif data.startswith("chat:"): await open_chat(update,context)
    elif data.startswith("report_menu:"): await report_menu(update,context)
    elif data.startswith("report:"): await report_user(update,context)
    elif data.startswith("block:"): await block_user(update,context)
    elif data=="delete_profile": await delete_confirm(update,context)
    elif data=="delete_yes": await delete_profile(update,context)
    elif data.startswith("admin:"): await admin_callback(update,context)
    elif data=="home":
        q=update.callback_query; await q.answer(); context.user_data.clear(); await home(update,context)


async def error_handler(update,context):
    log.exception("Unhandled error",exc_info=context.error)
    if ADMIN_ID:
        try: await context.bot.send_message(ADMIN_ID,f"❌ Together error:\n{type(context.error).__name__}: {context.error}")
        except Exception: pass


def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN environment variable is missing.")
    init_db()
    app=Application.builder().token(BOT_TOKEN).build()
    conversation=ConversationHandler(
        entry_points=[CommandHandler("start",start),MessageHandler(filters.Regex("^✏️ Խմբագրել պրոֆիլը$"),edit_profile)],
        states={
            NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND,name_step)],
            AGE:[MessageHandler(filters.TEXT & ~filters.COMMAND,age_step)],
            CITY:[MessageHandler(filters.TEXT & ~filters.COMMAND,city_step)],
            GENDER:[MessageHandler(filters.TEXT & ~filters.COMMAND,gender_step)],
            LOOKING_FOR:[MessageHandler(filters.TEXT & ~filters.COMMAND,looking_step)],
            ABOUT:[MessageHandler(filters.TEXT & ~filters.COMMAND,about_step)],
            PHOTO:[MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND),photo_step)],
        },
        fallbacks=[CommandHandler("cancel",cancel),MessageHandler(filters.Regex("^⬅️ Չեղարկել$"),cancel)],
        allow_reentry=True,
    )
    app.add_handler(conversation)
    app.add_handler(CommandHandler("admin",admin_menu))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_router))
    app.add_error_handler(error_handler)
    if app.job_queue is None:
        raise RuntimeError('JobQueue is unavailable. Install: python-telegram-bot[job-queue]')
    app.job_queue.run_repeating(inactivity_cleanup,interval=30,first=30)
    log.info("Together bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__=="__main__": main()

import os
import sqlite3
import logging
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
# TOGETHR — TELEGRAM DATING BOT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_FILE = "togethr.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("Togethr")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():

    connection = get_db()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            name TEXT DEFAULT '',
            age INTEGER,
            city TEXT DEFAULT '',
            gender TEXT DEFAULT '',
            looking_for TEXT DEFAULT '',
            about TEXT DEFAULT '',
            photo_file_id TEXT,
            banned INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS swipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            action TEXT NOT NULL,
            UNIQUE(from_user, to_user)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1 INTEGER NOT NULL,
            user2 INTEGER NOT NULL,
            created_at TEXT,
            UNIQUE(user1, user2)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter INTEGER NOT NULL,
            reported INTEGER NOT NULL,
            reason TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT
        )
    """)

    connection.commit()
    connection.close()


# ============================================================
# USER DATABASE FUNCTIONS
# ============================================================

def create_user(telegram_user):

    connection = get_db()

    connection.execute("""
        INSERT OR IGNORE INTO users
        (
            id,
            username,
            name,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        telegram_user.id,
        telegram_user.username or "",
        telegram_user.first_name or "",
        datetime.utcnow().isoformat(),
    ))

    connection.commit()
    connection.close()


def get_user(user_id):

    connection = get_db()

    user = connection.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    connection.close()

    return user


def update_user(user_id, field, value):

    allowed_fields = {
        "name",
        "age",
        "city",
        "gender",
        "looking_for",
        "about",
        "photo_file_id",
    }

    if field not in allowed_fields:
        return

    connection = get_db()

    connection.execute(
        f"UPDATE users SET {field} = ? WHERE id = ?",
        (value, user_id)
    )

    connection.commit()
    connection.close()


def is_banned(user_id):

    user = get_user(user_id)

    if not user:
        return False

    return bool(user["banned"])


def profile_is_complete(user):

    if not user:
        return False

    return all([
        user["name"],
        user["age"],
        user["city"],
        user["gender"],
        user["looking_for"],
        user["photo_file_id"],
    ])


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👤 Իմ պրոֆիլը",
                callback_data="profile"
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 Գտնել մարդկանց",
                callback_data="discover"
            )
        ],
        [
            InlineKeyboardButton(
                "❤️ Իմ Match-երը",
                callback_data="matches"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Խմբագրել պրոֆիլը",
                callback_data="edit_profile"
            )
        ],
    ])


def profile_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✏️ Խմբագրել",
                callback_data="edit_profile"
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 Գտնել մարդկանց",
                callback_data="discover"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Գլխավոր",
                callback_data="home"
            )
        ],
    ])


def swipe_keyboard(user_id):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Չհավանել",
                callback_data=f"pass:{user_id}"
            ),
            InlineKeyboardButton(
                "❤️ Հավանել",
                callback_data=f"like:{user_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "⭐ Super Like",
                callback_data=f"super:{user_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🚨 Բողոքել",
                callback_data=f"report:{user_id}"
            )
        ],
    ])


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    telegram_user = update.effective_user

    create_user(telegram_user)

    context.user_data.pop("editing", None)
    context.user_data.pop("step", None)
    context.user_data.pop("reporting", None)
    context.user_data.pop("report_target", None)

    if is_banned(telegram_user.id):

        await update.message.reply_text(
            "🚫 Ձեր հաշիվը արգելափակված է։"
        )

        return

    await update.message.reply_text(
        """
💖 <b>Բարի գալուստ Togethr</b>

Գտիր քո մարդուն ❤️

Togethr-ում կարող ես՝

👤 Ստեղծել քո պրոֆիլը
📸 Ավելացնել լուսանկար
❤️ Հավանել մարդկանց
❌ Բաց թողնել
⭐ Ուղարկել Super Like
🎉 Ստանալ Match
💬 Զրուցել Match-իդ հետ

Ամեն ինչ՝ հենց Telegram-ում։
        """,
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# PROFILE CREATION / EDITING
# ============================================================

async def start_profile(query, context):

    user_id = query.from_user.id

    context.user_data.clear()

    context.user_data["editing"] = True
    context.user_data["step"] = "name"

    await query.edit_message_text(
        """
👤 <b>Ստեղծենք քո Togethr պրոֆիլը</b>

1️⃣ Գրիր քո անունը։
        """,
        parse_mode="HTML"
    )


async def profile_text_handler(update, context):

    if not update.message:
        return

    if not context.user_data.get("editing"):
        return

    user_id = update.effective_user.id

    step = context.user_data.get("step")

    text = update.message.text.strip()

    if not text:
        return

    # --------------------------------------------------------
    # NAME
    # --------------------------------------------------------

    if step == "name":

        if len(text) < 2:

            await update.message.reply_text(
                "❗ Անունը պետք է լինի առնվազն 2 նիշ։"
            )

            return

        if len(text) > 40:

            await update.message.reply_text(
                "❗ Անունը շատ երկար է։"
            )

            return

        update_user(
            user_id,
            "name",
            text
        )

        context.user_data["step"] = "age"

        await update.message.reply_text(
            """
🎂 <b>Քանի՞ տարեկան ես։</b>

Մուտքագրիր թիվ՝ 18-ից 99։
            """,
            parse_mode="HTML"
        )

        return

    # --------------------------------------------------------
    # AGE
    # --------------------------------------------------------

    if step == "age":

        try:
            age = int(text)

        except ValueError:

            await update.message.reply_text(
                "❗ Տարիքը պետք է գրես թվով։"
            )

            return

        if age < 18 or age > 99:

            await update.message.reply_text(
                "❗ Togethr-ը նախատեսված է 18+ օգտատերերի համար։"
            )

            return

        update_user(
            user_id,
            "age",
            age
        )

        context.user_data["step"] = "city"

        await update.message.reply_text(
            """
📍 <b>Ո՞ր քաղաքում ես ապրում։</b>

Օրինակ՝ Երևան։
            """,
            parse_mode="HTML"
        )

        return

    # --------------------------------------------------------
    # CITY
    # --------------------------------------------------------

    if step == "city":

        if len(text) > 100:

            await update.message.reply_text(
                "❗ Քաղաքի անունը շատ երկար է։"
            )

            return

        update_user(
            user_id,
            "city",
            text
        )

        context.user_data["step"] = "gender"

        await update.message.reply_text(
            """
⚧️ <b>Քո սեռը</b>

Գրիր՝

👨 Տղամարդ
👩 Կին
🧑 Այլ
            """,
            parse_mode="HTML"
        )

        return

    # --------------------------------------------------------
    # GENDER
    # --------------------------------------------------------

    if step == "gender":

        valid = [
            "տղամարդ",
            "կին",
            "այլ",
            "👨 տղամարդ",
            "👩 կին",
            "🧑 այլ",
        ]

        if text.lower() not in valid:

            await update.message.reply_text(
                "❗ Գրիր՝ Տղամարդ, Կին կամ Այլ։"
            )

            return

        clean_gender = text.replace(
            "👨 ", ""
        ).replace(
            "👩 ", ""
        ).replace(
            "🧑 ", ""
        ).capitalize()

        update_user(
            user_id,
            "gender",
            clean_gender
        )

        context.user_data["step"] = "looking"

        await update.message.reply_text(
            """
❤️ <b>Ու՞մ ես փնտրում։</b>

Գրիր՝

👨 Տղամարդ
👩 Կին
❤️ Բոլորը
            """,
            parse_mode="HTML"
        )

        return

    # --------------------------------------------------------
    # LOOKING FOR
    # --------------------------------------------------------

    if step == "looking":

        value = text.lower()

        if value in ["տղամարդ", "👨 տղամարդ"]:

            looking = "Տղամարդ"

        elif value in ["կին", "👩 կին"]:

            looking = "Կին"

        elif value in ["բոլորը", "❤️ բոլորը"]:

            looking = "Բոլորը"

        else:

            await update.message.reply_text(
                "❗ Գրիր՝ Տղամարդ, Կին կամ Բոլորը։"
            )

            return

        update_user(
            user_id,
            "looking_for",
            looking
        )

        context.user_data["step"] = "about"

        await update.message.reply_text(
            """
📝 <b>Պատմիր մի փոքր քո մասին։</b>

Օրինակ՝

«Սիրում եմ ճանապարհորդել, երաժշտություն
լսել և նոր մարդկանց ճանաչել»։

Կարող ես նաև գրել, թե ինչ ես փնտրում։
            """,
            parse_mode="HTML"
        )

        return

    # --------------------------------------------------------
    # ABOUT
    # --------------------------------------------------------

    if step == "about":

        if len(text) > 500:

            await update.message.reply_text(
                "❗ Նկարագրությունը կարող է լինել մինչև 500 նիշ։"
            )

            return

        update_user(
            user_id,
            "about",
            text
        )

        context.user_data["step"] = "photo"

        await update.message.reply_text(
            """
📸 <b>Վերջին քայլը</b>

Ուղարկիր քո պրոֆիլի լուսանկարը։

Լավագույնը՝ պարզ և իրական լուսանկարն է։
            """,
            parse_mode="HTML"
        )

        return


# ============================================================
# PROFILE PHOTO
# ============================================================

async def profile_photo_handler(update, context):

    if not update.message:
        return

    if not context.user_data.get("editing"):
        return

    if context.user_data.get("step") != "photo":
        return

    if not update.message.photo:

        await update.message.reply_text(
            "📸 Խնդրում եմ ուղարկիր լուսանկար։"
        )

        return

    user_id = update.effective_user.id

    photo = update.message.photo[-1]

    update_user(
        user_id,
        "photo_file_id",
        photo.file_id
    )

    context.user_data.clear()

    await update.message.reply_text(
        """
🎉 <b>Պրոֆիլդ պատրաստ է։</b>

Այժմ կարող ես սկսել ծանոթանալ մարդկանց հետ։ ❤️
        """,
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# SHOW OWN PROFILE
# ============================================================

async def show_my_profile(query):

    user_id = query.from_user.id

    user = get_user(user_id)

    if not user:
        return

    about = (
        user["about"]
        if user["about"]
        else "Նկարագրություն չկա։"
    )

    text = (
        f"👤 <b>{user['name'] or 'Անուն չկա'}</b>\n\n"
        f"🎂 Տարիք՝ {user['age'] or '-'}\n"
        f"📍 Քաղաք՝ {user['city'] or '-'}\n"
        f"⚧️ Սեռ՝ {user['gender'] or '-'}\n"
        f"❤️ Փնտրում է՝ {user['looking_for'] or '-'}\n\n"
        f"📝 {about}"
    )

    if user["photo_file_id"]:

        try:

            await query.message.reply_photo(
                photo=user["photo_file_id"],
                caption=text,
                parse_mode="HTML",
                reply_markup=profile_keyboard()
            )

            await query.delete_message()

            return

        except Exception as error:

            logger.error(
                "Profile photo error: %s",
                error
            )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=profile_keyboard()
    )


# ============================================================
# DISCOVERY
# ============================================================

def get_next_profile(user_id):

    connection = get_db()

    profile = connection.execute("""
        SELECT *
        FROM users
        WHERE id != ?
        AND banned = 0
        AND age IS NOT NULL
        AND photo_file_id IS NOT NULL

        AND id NOT IN (
            SELECT to_user
            FROM swipes
            WHERE from_user = ?
        )

        ORDER BY RANDOM()
        LIMIT 1
    """, (
        user_id,
        user_id
    )).fetchone()

    connection.close()

    return profile


def compatible(viewer, candidate):

    if not viewer or not candidate:
        return False

    viewer_preference = (
        viewer["looking_for"] or ""
    ).lower()

    candidate_gender = (
        candidate["gender"] or ""
    ).lower()

    if viewer_preference not in (
        "",
        "բոլորը",
        "բոլորին",
    ):

        if viewer_preference != candidate_gender:
            return False

    return True


async def discover(query, context):

    user_id = query.from_user.id

    user = get_user(user_id)

    if not profile_is_complete(user):

        await query.edit_message_text(
            """
⚠️ <b>Պրոֆիլդ դեռ ամբողջական չէ։</b>

Նախ լրացրու անունը, տարիքը, քաղաքը,
սեռը, նախընտրությունը և ավելացրու լուսանկար։
            """,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "👤 Լրացնել պրոֆիլը",
                        callback_data="edit_profile"
                    )
                ]
            ])
        )

        return

    profile = get_next_profile(user_id)

    # Փնտրում ենք համապատասխան պրոֆիլ
    attempts = 0

    while profile and not compatible(
        user,
        profile
    ) and attempts < 20:

        # ժամանակավոր նշում, որպեսզի նույնը չվերադարձնի
        save_swipe(
            user_id,
            profile["id"],
            "skip_auto"
        )

        profile = get_next_profile(
            user_id
        )

        attempts += 1

    if not profile:

        await query.edit_message_text(
            """
💜 <b>Այս պահին նոր պրոֆիլներ չկան։</b>

Փորձիր մի փոքր հետո։
            """,
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

        return

    about = (
        profile["about"]
        if profile["about"]
        else "Նկարագրություն չկա։"
    )

    text = (
        f"👤 <b>{profile['name']}, "
        f"{profile['age']}</b>\n\n"
        f"📍 {profile['city']}\n"
        f"⚧️ {profile['gender']}\n"
        f"❤️ Փնտրում է՝ {profile['looking_for']}\n\n"
        f"📝 {about}"
    )

    try:

        await query.message.reply_photo(
            photo=profile["photo_file_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=swipe_keyboard(
                profile["id"]
            )
        )

        await query.delete_message()

    except Exception as error:

        logger.error(
            "Discovery error: %s",
            error
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=swipe_keyboard(
                profile["id"]
            )
        )


# ============================================================
# SWIPES
# ============================================================

def save_swipe(
    from_user,
    to_user,
    action
):

    connection = get_db()

    connection.execute("""
        INSERT INTO swipes
        (
            from_user,
            to_user,
            action
        )
        VALUES (?, ?, ?)

        ON CONFLICT(from_user, to_user)
        DO UPDATE SET action = excluded.action
    """, (
        from_user,
        to_user,
        action
    ))

    connection.commit()
    connection.close()


def has_reciprocal_like(
    user1,
    user2
):

    connection = get_db()

    result = connection.execute("""
        SELECT id
        FROM swipes
        WHERE from_user = ?
        AND to_user = ?
        AND action IN ('like', 'super')
        LIMIT 1
    """, (
        user2,
        user1
    )).fetchone()

    connection.close()

    return bool(result)


# ============================================================
# MATCH
# ============================================================

def create_match(
    user1,
    user2
):

    a, b = sorted([
        user1,
        user2
    ])

    connection = get_db()

    connection.execute("""
        INSERT OR IGNORE INTO matches
        (
            user1,
            user2,
            created_at
        )
        VALUES (?, ?, ?)
    """, (
        a,
        b,
        datetime.utcnow().isoformat()
    ))

    connection.commit()

    match = connection.execute("""
        SELECT *
        FROM matches
        WHERE user1 = ?
        AND user2 = ?
    """, (
        a,
        b
    )).fetchone()

    connection.close()

    return match


async def handle_swipe(
    query,
    context
):

    action, target = query.data.split(":")

    target_id = int(target)

    user_id = query.from_user.id

    if user_id == target_id:

        await query.answer(
            "❗ Չես կարող քեզ հավանել։",
            show_alert=True
        )

        return

    save_swipe(
        user_id,
        target_id,
        action
    )

    if action in (
        "like",
        "super"
    ):

        if has_reciprocal_like(
            user_id,
            target_id
        ):

            match = create_match(
                user_id,
                target_id
            )

            me = get_user(
                user_id
            )

            other = get_user(
                target_id
            )

            await query.answer(
                "🎉 Դուք Match եք!",
                show_alert=True
            )

            match_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💬 Գրել",
                        callback_data=f"chat:{match['id']}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔎 Շարունակել",
                        callback_data="discover"
                    )
                ]
            ])

            try:

                await query.edit_message_caption(
                    caption=(
                        "🎉 <b>MATCH!</b>\n\n"
                        f"Դու և <b>{other['name']}</b>-ը "
                        "հավանել եք միմյանց։ ❤️\n\n"
                        "Այժմ կարող եք զրուցել։"
                    ),
                    parse_mode="HTML",
                    reply_markup=match_keyboard
                )

            except Exception:

                pass

            try:

                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        "🎉 <b>Նոր Match!</b>\n\n"
                        f"Դու և <b>{me['name']}</b>-ը "
                        "հավանել եք միմյանց։ ❤️"
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "💬 Գրել",
                                callback_data=f"chat:{match['id']}"
                            )
                        ]
                    ])
                )

            except Exception as error:

                logger.error(
                    "Match notification error: %s",
                    error
                )

            return

        if action == "super":

            await query.answer(
                "⭐ Super Like ուղարկվեց!"
            )

        else:

            await query.answer(
                "❤️ Հավանումը պահպանվեց!"
            )

    elif action == "pass":

        await query.answer(
            "❌ Բաց թողեցիր։"
        )

    await discover(
        query,
        context
    )


# ============================================================
# MATCH LIST
# ============================================================

def get_user_matches(user_id):

    connection = get_db()

    matches = connection.execute("""
        SELECT *
        FROM matches
        WHERE user1 = ?
        OR user2 = ?
        ORDER BY id DESC
    """, (
        user_id,
        user_id
    )).fetchall()

    connection.close()

    return matches


async def show_matches(query):

    user_id = query.from_user.id

    matches = get_user_matches(
        user_id
    )

    if not matches:

        await query.edit_message_text(
            """
❤️ <b>Իմ Match-երը</b>

Դեռ Match չունես։

Գնա «Գտնել մարդկանց» և սկսիր ծանոթանալ։
            """,
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

        return

    buttons = []

    for match in matches:

        if match["user1"] == user_id:

            other_id = match["user2"]

        else:

            other_id = match["user1"]

        other = get_user(
            other_id
        )

        if not other:
            continue

        buttons.append([
            InlineKeyboardButton(
                f"💬 {other['name']}",
                callback_data=f"chat:{match['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔎 Նոր ծանոթություններ",
            callback_data="discover"
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Գլխավոր",
            callback_data="home"
        )
    ])

    await query.edit_message_text(
        """
❤️ <b>Իմ Match-երը</b>

Ընտրիր այն մարդուն, ում հետ ցանկանում ես զրուցել։
        """,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# CHAT
# ============================================================

def get_match_for_user(
    match_id,
    user_id
):

    connection = get_db()

    match = connection.execute("""
        SELECT *
        FROM matches
        WHERE id = ?
        AND (
            user1 = ?
            OR user2 = ?
        )
    """, (
        match_id,
        user_id,
        user_id
    )).fetchone()

    connection.close()

    return match


async def open_chat(
    query,
    context,
    match_id
):

    user_id = query.from_user.id

    match = get_match_for_user(
        match_id,
        user_id
    )

    if not match:

        await query.answer(
            "❗ Այս Match-ը հասանելի չէ։",
            show_alert=True
        )

        return

    if match["user1"] == user_id:

        other_id = match["user2"]

    else:

        other_id = match["user1"]

    other = get_user(
        other_id
    )

    if not other:

        return

    context.user_data[
        "chat_match_id"
    ] = match_id

    await query.edit_message_text(
        f"""
💬 <b>Զրույց {other['name']}-ի հետ</b>

Դու հիմա կարող ես ուղարկել հաղորդագրություններ։

🔒 Telegram ID-ները չեն ցուցադրվում։
        """,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Match-եր",
                    callback_data="matches"
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Գլխավոր",
                    callback_data="home"
                )
            ]
        ])
    )


async def chat_message(
    update,
    context
):

    match_id = context.user_data.get(
        "chat_match_id"
    )

    if not match_id:
        return

    if not update.message:
        return

    if not update.message.text:
        return

    user_id = update.effective_user.id

    match = get_match_for_user(
        match_id,
        user_id
    )

    if not match:
        context.user_data.pop(
            "chat_match_id",
            None
        )
        return

    if match["user1"] == user_id:

        other_id = match["user2"]

    else:

        other_id = match["user1"]

    text = update.message.text.strip()

    if not text:
        return

    if len(text) > 1000:

        await update.message.reply_text(
            "❗ Հաղորդագրությունը կարող է լինել մինչև 1000 նիշ։"
        )

        return

    connection = get_db()

    connection.execute("""
        INSERT INTO messages
        (
            match_id,
            sender_id,
            text,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        match_id,
        user_id,
        text,
        datetime.utcnow().isoformat()
    ))

    connection.commit()
    connection.close()

    try:

        await context.bot.send_message(
            chat_id=other_id,
            text=(
                "💬 <b>Նոր հաղորդագրություն</b>\n\n"
                f"{text}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💬 Պատասխանել",
                        callback_data=f"chat:{match_id}"
                    )
                ]
            ])
        )

        await update.message.reply_text(
            "✅ Ուղարկվեց։"
        )

    except Exception as error:

        logger.error(
            "Chat error: %s",
            error
        )

        await update.message.reply_text(
            "❗ Հաղորդագրությունը չհաջողվեց ուղարկել։"
        )


# ============================================================
# REPORT SYSTEM
# ============================================================

async def start_report(
    query,
    context,
    target_id
):

    context.user_data[
        "report_target"
    ] = target_id

    context.user_data[
        "reporting"
    ] = True

    context.user_data.pop(
        "chat_match_id",
        None
    )

    await query.edit_message_text(
        """
🚨 <b>Բողոք օգտատիրոջ դեմ</b>

Գրիր բողոքի պատճառը։

Օրինակ՝
• Կեղծ պրոֆիլ
• Վիրավորանք
• Անպատշաճ վարք
• Սպամ
• Այլ
        """,
        parse_mode="HTML"
    )


async def report_message(
    update,
    context
):

    if not context.user_data.get(
        "reporting"
    ):
        return

    if not update.message:
        return

    if not update.message.text:
        return

    reporter = update.effective_user.id

    target = context.user_data.pop(
        "report_target",
        None
    )

    context.user_data.pop(
        "reporting",
        None
    )

    if not target:
        return

    reason = update.message.text.strip()

    connection = get_db()

    connection.execute("""
        INSERT INTO reports
        (
            reporter,
            reported,
            reason,
            status,
            created_at
        )
        VALUES (?, ?, ?, 'open', ?)
    """, (
        reporter,
        target,
        reason[:1000],
        datetime.utcnow().isoformat()
    ))

    connection.commit()
    connection.close()

    await update.message.reply_text(
        """
✅ <b>Բողոքը ստացվեց։</b>

Ադմինը կստուգի այն։
Շնորհակալություն Togethr-ը անվտանգ պահելու համար։ ❤️
        """,
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# ADMIN
# ============================================================

def is_admin(user_id):

    return (
        ADMIN_ID != 0
        and user_id == ADMIN_ID
    )


async def admin_command(
    update,
    context
):

    user_id = update.effective_user.id

    if not is_admin(user_id):

        await update.message.reply_text(
            "🚫 Մուտքը թույլատրված չէ։"
        )

        return

    connection = get_db()

    total_users = connection.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    total_profiles = connection.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE photo_file_id IS NOT NULL
    """).fetchone()[0]

    total_matches = connection.execute(
        "SELECT COUNT(*) FROM matches"
    ).fetchone()[0]

    total_reports = connection.execute("""
        SELECT COUNT(*)
        FROM reports
        WHERE status = 'open'
    """).fetchone()[0]

    banned_users = connection.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE banned = 1
    """).fetchone()[0]

    total_messages = connection.execute(
        "SELECT COUNT(*) FROM messages"
    ).fetchone()[0]

    connection.close()

    await update.message.reply_text(
        f"""
👨‍💼 <b>Togethr Admin Panel</b>

👥 Օգտատերեր՝ <b>{total_users}</b>
👤 Լրացված պրոֆիլներ՝ <b>{total_profiles}</b>
❤️ Match-եր՝ <b>{total_matches}</b>
💬 Հաղորդագրություններ՝ <b>{total_messages}</b>
🚨 Բաց բողոքներ՝ <b>{total_reports}</b>
🚫 Արգելափակվածներ՝ <b>{banned_users}</b>

<b>Admin հրամաններ</b>

/admin — վիճակագրություն
/ban ID — արգելափակել
/unban ID — ապաշրջափակել
/reports — բողոքներ
        """,
        parse_mode="HTML"
    )


async def ban_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
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
            "❗ ID-ն պետք է թիվ լինի։"
        )

        return

    connection = get_db()

    connection.execute(
        "UPDATE users SET banned = 1 WHERE id = ?",
        (target_id,)
    )

    connection.commit()
    connection.close()

    await update.message.reply_text(
        f"🚫 Օգտատեր {target_id}-ը արգելափակվեց։"
    )


async def unban_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
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
            "❗ ID-ն պետք է թիվ լինի։"
        )

        return

    connection = get_db()

    connection.execute(
        "UPDATE users SET banned = 0 WHERE id = ?",
        (target_id,)
    )

    connection.commit()
    connection.close()

    await update.message.reply_text(
        f"✅ Օգտատեր {target_id}-ը ապաշրջափակվեց։"
    )


async def reports_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    connection = get_db()

    reports = connection.execute("""
        SELECT *
        FROM reports
        WHERE status = 'open'
        ORDER BY id DESC
        LIMIT 20
    """).fetchall()

    connection.close()

    if not reports:

        await update.message.reply_text(
            "✅ Բաց բողոքներ չկան։"
        )

        return

    for report in reports:

        await update.message.reply_text(
            f"""
🚨 <b>Բողոք #{report['id']}</b>

👤 Բողոքող՝ <code>{report['reporter']}</code>
👤 Բողոքարկվող՝ <code>{report['reported']}</code>

📝 Պատճառ՝
{report['reason']}

/ban {report['reported']}
            """,
            parse_mode="HTML"
        )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    data = query.data

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    if data == "home":

        await query.edit_message_text(
            """
💖 <b>Բարի գալուստ Togethr</b>

Գտիր քո մարդուն ❤️
            """,
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

        return

    # --------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------

    if data == "profile":

        await show_my_profile(
            query
        )

        return

    # --------------------------------------------------------
    # EDIT PROFILE
    # --------------------------------------------------------

    if data == "edit_profile":

        await start_profile(
            query,
            context
        )

        return

    # --------------------------------------------------------
    # DISCOVER
    # --------------------------------------------------------

    if data == "discover":

        await discover(
            query,
            context
        )

        return

    # --------------------------------------------------------
    # MATCHES
    # --------------------------------------------------------

    if data == "matches":

        await show_matches(
            query
        )

        return

    # --------------------------------------------------------
    # SWIPES
    # --------------------------------------------------------

    if data.startswith("like:"):

        await handle_swipe(
            query,
            context
        )

        return

    if data.startswith("pass:"):

        await handle_swipe(
            query,
            context
        )

        return

    if data.startswith("super:"):

        await handle_swipe(
            query,
            context
        )

        return

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    if data.startswith("report:"):

        target_id = int(
            data.split(":")[1]
        )

        await start_report(
            query,
            context,
            target_id
        )

        return

    # --------------------------------------------------------
    # CHAT
    # --------------------------------------------------------

    if data.startswith("chat:"):

        match_id = int(
            data.split(":")[1]
        )

        await open_chat(
            query,
            context,
            match_id
        )

        return


# ============================================================
# GENERAL TEXT ROUTER
# ============================================================

async def text_router(
    update,
    context
):

    if not update.message:
        return

    user_id = update.effective_user.id

    if is_banned(user_id):
        return

    # Report-ը ունի ամենաբարձր առաջնահերթություն
    if context.user_data.get("reporting"):

        await report_message(
            update,
            context
        )

        return

    # Chat mode
    if context.user_data.get("chat_match_id"):

        await chat_message(
            update,
            context
        )

        return

    # Profile creation/editing
    if context.user_data.get("editing"):

        await profile_text_handler(
            update,
            context
        )

        return


# ============================================================
# COMMANDS
# ============================================================

async def help_command(
    update,
    context
):

    await update.message.reply_text(
        """
💖 <b>Togethr օգնություն</b>

👤 /start — սկսել
❤️ Գտնել Match
💬 Զրուցել Match-ի հետ
👤 Ստեղծել կամ փոխել պրոֆիլը

Եթե խնդիր ունես, օգտագործիր բողոքարկման համակարգը։
        """,
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "Exception while handling update:",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN-ը սահմանված չէ։"
        )

    init_database()

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    application.add_handler(
        CommandHandler(
            "ban",
            ban_command
        )
    )

    application.add_handler(
        CommandHandler(
            "unban",
            unban_command
        )
    )

    application.add_handler(
        CommandHandler(
            "reports",
            reports_command
        )
    )

    # Callback buttons
    application.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # Profile photos
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            profile_photo_handler
        )
    )

    # Text
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "💖 Togethr bot started successfully."
    )

    print(
        "💖 Togethr bot-ը գործարկվեց..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()

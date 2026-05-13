import asyncio
import logging
import aiosqlite
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ================= CONFIG =================

TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# LOGGER TANIMLAMASI - DEBUG LOGUNDAN ÖNCE
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# DEBUG: Environment variables kontrolü
logger.info(f"DEBUG: BOT_TOKEN={TOKEN}")
logger.info(f"DEBUG: CHANNEL_ID={CHANNEL_ID}")
logger.info(f"DEBUG: CHANNEL_USERNAME={CHANNEL_USERNAME}")
logger.info(f"DEBUG: ADMIN_ID={ADMIN_ID}")

# Eksik değer kontrolü
if not TOKEN:
    logger.error("BOT_TOKEN bos! Bot ishlamaydi.")
if not CHANNEL_ID:
    logger.error("CHANNEL_ID bos!")
if not CHANNEL_USERNAME:
    logger.error("CHANNEL_USERNAME bos!")
if not ADMIN_ID:
    logger.error("ADMIN_ID bos!")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ================= FSM STATES =================

class ContestCreate(StatesGroup):
    waiting_title    = State()
    waiting_media    = State()
    waiting_count    = State()
    waiting_variants = State()


# ================= DATABASE =================

async def db_start():
    async with aiosqlite.connect("database.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contests (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL,
                active     INTEGER DEFAULT 1,
                message_id INTEGER,
                media_id   TEXT,
                media_type TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS variants (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                contest_id INTEGER NOT NULL,
                name       TEXT NOT NULL,
                votes      INTEGER DEFAULT 0,
                FOREIGN KEY (contest_id) REFERENCES contests(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users_votes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                contest_id INTEGER NOT NULL,
                variant_id INTEGER NOT NULL,
                UNIQUE(user_id, contest_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_votes (
                user_id    INTEGER PRIMARY KEY,
                contest_id INTEGER NOT NULL,
                variant_id INTEGER NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_contests_active ON contests(active)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_variants_contest ON variants(contest_id)")

        for col in ["media_id TEXT", "media_type TEXT"]:
            try:
                await db.execute(f"ALTER TABLE contests ADD COLUMN {col}")
            except Exception:
                pass

        await db.commit()
    logger.info("Database tayyor ✅")


# ================= CHECK SUB =================

async def check_sub(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"check_sub xatosi user_id={user_id}: {e}")
        return False


# ================= KEYBOARDS =================

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Tanlov yaratish", callback_data="create_contest")],
        [InlineKeyboardButton(text="📋 Tanlovlar",       callback_data="list_contests")],
    ])

def user_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Tanlovlar", callback_data="list_contests")]
    ])

def sub_check_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga o'tish", url=f"https://t.me/{CHANNEL_USERNAME}")],
        [InlineKeyboardButton(text="✅ Tekshirish",     callback_data="check_sub")],
    ])


# ================= HELPERS =================

async def show_menu(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👨‍💻 Admin panel", reply_markup=admin_menu_kb())
    else:
        await message.answer("🏆 Tanlov botiga xush kelibsiz!", reply_markup=user_menu_kb())


async def safe_edit(call: CallbackQuery, text: str, kb=None, parse_mode="HTML"):
    """Xabar turi (matn/rasm/video) dan qat'i nazar tahrirlaydi"""
    try:
        if call.message.photo or call.message.video:
            await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode=parse_mode)
        else:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"safe_edit xato: {e}")
        await call.message.answer(text, reply_markup=kb, parse_mode=parse_mode)


async def do_vote(user_id: int, contest_id: int, variant_id: int) -> str:
    """'ok' | 'already' | 'inactive' | 'error'"""
    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute("SELECT active FROM contests WHERE id=?", (contest_id,))
        contest = await cur.fetchone()
        if not contest or not contest[0]:
            return "inactive"

        cur = await db.execute(
            "SELECT id FROM users_votes WHERE user_id=? AND contest_id=?",
            (user_id, contest_id)
        )
        if await cur.fetchone():
            return "already"

        try:
            await db.execute(
                "INSERT INTO users_votes(user_id, contest_id, variant_id) VALUES(?,?,?)",
                (user_id, contest_id, variant_id)
            )
            await db.execute(
                "UPDATE variants SET votes = votes + 1 WHERE id=?", (variant_id,)
            )
            await db.execute("DELETE FROM pending_votes WHERE user_id=?", (user_id,))
            await db.commit()
            return "ok"
        except Exception as e:
            logger.error(f"do_vote xato: {e}")
            return "error"


async def send_contest_to_channel(
    contest_id: int, title: str, variants: list,
    media_id: str = None, media_type: str = None
) -> int:
    bot_info = await bot.get_me()
    buttons = [
        [InlineKeyboardButton(
            text=f"🗳 {name}",
            url=f"https://t.me/{bot_info.username}?start=vote_{contest_id}_{vid}"
        )]
        for vid, name in variants
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    variant_list = "\n".join(f"  • {n}" for _, n in variants)
    caption = (
        f"🏆 <b>{title}</b>\n\n"
        f"Variantlar:\n{variant_list}\n\n"
        f"👇 Ovoz berish uchun variantni tanlang:"
    )

    if media_id and media_type == "photo":
        post = await bot.send_photo(CHANNEL_ID, photo=media_id,
                                    caption=caption, reply_markup=kb, parse_mode="HTML")
    elif media_id and media_type == "video":
        post = await bot.send_video(CHANNEL_ID, video=media_id,
                                    caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        post = await bot.send_message(CHANNEL_ID, caption, reply_markup=kb, parse_mode="HTML")

    return post.message_id


# ================= START =================

@dp.message(CommandStart(deep_link=False))
async def start(message: Message, state: FSMContext):
    await state.clear()
    if not await check_sub(message.from_user.id):
        await message.answer(
            "Botdan foydalanish uchun kanalga obuna bo'ling 👇",
            reply_markup=sub_check_kb()
        )
        return
    await show_menu(message)


@dp.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("👨‍💻 Admin panel", reply_markup=admin_menu_kb())


# ================= ADMIN PANEL CALLBACK =================

@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("❌ Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    await safe_edit(call, "👨‍💻 Admin panel", admin_menu_kb())
    await call.answer()


# ================= CHECK SUB CALLBACK =================

@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(call: CallbackQuery):
    if not await check_sub(call.from_user.id):
        await call.answer("❌ Avval kanalga obuna bo'ling!", show_alert=True)
        return

    await call.message.delete()

    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute(
            "SELECT contest_id, variant_id FROM pending_votes WHERE user_id=?",
            (call.from_user.id,)
        )
        pending = await cur.fetchone()

    if pending:
        contest_id, variant_id = pending
        async with aiosqlite.connect("database.db") as db:
            cur = await db.execute("SELECT title FROM contests WHERE id=?", (contest_id,))
            contest = await cur.fetchone()
            cur = await db.execute("SELECT name FROM variants WHERE id=?", (variant_id,))
            variant = await cur.fetchone()

        result = await do_vote(call.from_user.id, contest_id, variant_id)

        if result == "ok":
            await call.message.answer(
                f"✅ Obuna tasdiqlandi va ovozingiz hisoblandi!\n\n"
                f"🏆 <b>{contest[0] if contest else '?'}</b>\n"
                f"🗳 Tanlovingiz: <b>{variant[0] if variant else '?'}</b>",
                parse_mode="HTML", reply_markup=user_menu_kb()
            )
            return
        elif result == "already":
            await call.message.answer(
                "ℹ️ Siz bu tanlovga allaqachon ovoz bergansiz.",
                reply_markup=user_menu_kb()
            )
            return
        elif result == "inactive":
            await call.message.answer(
                "❌ Afsuski, bu tanlov tugagan.", reply_markup=user_menu_kb()
            )
            return

    if call.from_user.id == ADMIN_ID:
        await call.message.answer("👨‍💻 Admin panel", reply_markup=admin_menu_kb())
    else:
        await call.message.answer("🏆 Tanlov botiga xush kelibsiz!", reply_markup=user_menu_kb())


# ================= CREATE CONTEST =================

@dp.callback_query(F.data == "create_contest")
async def cb_create_contest(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("❌ Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(ContestCreate.waiting_title)
    await call.message.answer("📝 Yangi tanlov nomini yuboring:\n\n/bekor — bekor qilish")
    await call.answer()


@dp.message(ContestCreate.waiting_title, F.text)
async def fsm_get_title(message: Message, state: FSMContext):
    if message.text.strip() == "/bekor":
        await state.clear()
        await message.answer("❌ Bekor qilindi", reply_markup=admin_menu_kb())
        return
    title = message.text.strip()
    if not title:
        await message.answer("❌ Nom bo'sh bo'lmasin:")
        return
    await state.update_data(title=title, variants=[], current=0, count=0,
                             media_id=None, media_type=None)
    await state.set_state(ContestCreate.waiting_media)
    await message.answer(
        f"✅ Nom: <b>{title}</b>\n\n"
        f"📸 Rasm yoki 🎥 Video yuboring (ixtiyoriy)\n"
        f"Mediasiz davom ettirish: /otkazib\n\n"
        f"/bekor — bekor qilish",
        parse_mode="HTML"
    )


@dp.message(ContestCreate.waiting_media, F.photo)
async def fsm_get_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    await state.update_data(media_id=photo.file_id, media_type="photo")
    await _ask_count(message, state)


@dp.message(ContestCreate.waiting_media, F.video)
async def fsm_get_video(message: Message, state: FSMContext):
    await state.update_data(media_id=message.video.file_id, media_type="video")
    await _ask_count(message, state)


@dp.message(ContestCreate.waiting_media, F.text)
async def fsm_skip_media(message: Message, state: FSMContext):
    txt = message.text.strip()
    if txt == "/bekor":
        await state.clear()
        await message.answer("❌ Bekor qilindi", reply_markup=admin_menu_kb())
        return
    if txt == "/otkazib":
        await state.update_data(media_id=None, media_type=None)
        await _ask_count(message, state)
        return
    await message.answer(
        "⚠️ Rasm yoki video yuboring.\n"
        "Mediasiz davom etish: /otkazib\n"
        "/bekor — bekor qilish"
    )


async def _ask_count(message: Message, state: FSMContext):
    data = await state.get_data()
    media_info = (
        "📸 Rasm qo'shildi ✅" if data.get("media_type") == "photo"
        else "🎥 Video qo'shildi ✅" if data.get("media_type") == "video"
        else "🚫 Mediasiz"
    )
    await state.set_state(ContestCreate.waiting_count)
    await message.answer(
        f"{media_info}\n\n🔢 Nechchi variant bo'ladi?\n\n/bekor — bekor qilish"
    )


@dp.message(ContestCreate.waiting_count, F.text)
async def fsm_get_count(message: Message, state: FSMContext):
    if message.text.strip() == "/bekor":
        await state.clear()
        await message.answer("❌ Bekor qilindi", reply_markup=admin_menu_kb())
        return
    try:
        count = int(message.text.strip())
        if count < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Musbat raqam kiriting (masalan: 3):")
        return
    await state.update_data(count=count)
    await state.set_state(ContestCreate.waiting_variants)
    await message.answer(f"1️⃣ 1-variantni yuboring (1/{count})\n\n/bekor — bekor qilish")


@dp.message(ContestCreate.waiting_variants, F.text)
async def fsm_get_variants(message: Message, state: FSMContext):
    if message.text.strip() == "/bekor":
        await state.clear()
        await message.answer("❌ Bekor qilindi", reply_markup=admin_menu_kb())
        return
    variant_name = message.text.strip()
    if not variant_name:
        await message.answer("❌ Bo'sh bo'lmasin:")
        return
    data = await state.get_data()
    variants = data["variants"]
    variants.append(variant_name)
    current = data["current"] + 1
    count = data["count"]
    await state.update_data(variants=variants, current=current)

    if current < count:
        await message.answer(
            f"✅ <b>{variant_name}</b> qabul qilindi\n\n"
            f"{current + 1}️⃣ Keyingi variant ({current + 1}/{count})\n\n/bekor — bekor qilish",
            parse_mode="HTML"
        )
    else:
        await state.clear()
        await do_create_contest(message, data["title"], variants,
                                data.get("media_id"), data.get("media_type"))


async def do_create_contest(message: Message, title: str, variants: list,
                             media_id: str = None, media_type: str = None):
    try:
        async with aiosqlite.connect("database.db") as db:
            cur = await db.execute(
                "INSERT INTO contests(title, media_id, media_type) VALUES(?,?,?)",
                (title, media_id, media_type)
            )
            contest_id = cur.lastrowid
            variant_rows = []
            for name in variants:
                c = await db.execute(
                    "INSERT INTO variants(contest_id, name) VALUES(?,?)", (contest_id, name)
                )
                variant_rows.append((c.lastrowid, name))
            await db.commit()

        msg_id = await send_contest_to_channel(
            contest_id, title, variant_rows, media_id, media_type
        )

        async with aiosqlite.connect("database.db") as db:
            await db.execute(
                "UPDATE contests SET message_id=? WHERE id=?", (msg_id, contest_id)
            )
            await db.commit()

        await message.answer(
            f"✅ Tanlov yaratildi va kanalga yuborildi!\n\n"
            f"📌 <b>{title}</b>\n🗂 Variantlar: {len(variants)} ta",
            reply_markup=admin_menu_kb(), parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"do_create_contest xato: {e}")
        await message.answer("❌ Xato yuz berdi, qayta urinib ko'ring.",
                             reply_markup=admin_menu_kb())


# ================= VOTE — deep link =================

@dp.message(CommandStart(deep_link=True))
async def vote_deep(message: Message, state: FSMContext):
    await state.clear()
    try:
        payload = message.text.split()[1]
        if not payload.startswith("vote_"):
            await show_menu(message)
            return
        parts = payload.split("_")
        contest_id = int(parts[1])
        variant_id = int(parts[2]) if len(parts) > 2 else None
    except (IndexError, ValueError):
        await show_menu(message)
        return

    if not await check_sub(message.from_user.id):
        if variant_id is not None:
            async with aiosqlite.connect("database.db") as db:
                await db.execute(
                    """INSERT INTO pending_votes(user_id, contest_id, variant_id)
                       VALUES(?,?,?)
                       ON CONFLICT(user_id) DO UPDATE
                       SET contest_id=excluded.contest_id,
                           variant_id=excluded.variant_id""",
                    (message.from_user.id, contest_id, variant_id)
                )
                await db.commit()
        await message.answer(
            "Ovoz berish uchun avval kanalga obuna bo'ling.\n\n"
            "Obuna bo'lgandan keyin ✅ Tekshirish tugmasini bosing — "
            "ovozingiz avtomatik hisoblanadi! 👇",
            reply_markup=sub_check_kb()
        )
        return

    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute(
            "SELECT title, active FROM contests WHERE id=?", (contest_id,)
        )
        contest = await cur.fetchone()
        if not contest:
            await message.answer("❌ Bunday tanlov topilmadi")
            return
        if not contest[1]:
            await message.answer("❌ Bu tanlov tugagan")
            return
        contest_title = contest[0]

        cur = await db.execute(
            "SELECT id FROM users_votes WHERE user_id=? AND contest_id=?",
            (message.from_user.id, contest_id)
        )
        if await cur.fetchone():
            await message.answer(
                f"ℹ️ Siz <b>{contest_title}</b> tanloviga allaqachon ovoz bergansiz.",
                parse_mode="HTML"
            )
            return

        if variant_id is None:
            cur = await db.execute(
                "SELECT id, name FROM variants WHERE contest_id=? ORDER BY id", (contest_id,)
            )
            var_list = await cur.fetchall()
            if not var_list:
                await message.answer("❌ Bu tanlovda variantlar yo'q")
                return
            buttons = [
                [InlineKeyboardButton(
                    text=f"🗳 {name}", callback_data=f"vv_{vid}_{contest_id}"
                )]
                for vid, name in var_list
            ]
            await message.answer(
                f"🏆 <b>{contest_title}</b>\n\nVariantni tanlang:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="HTML"
            )
            return

        cur = await db.execute(
            "SELECT name FROM variants WHERE id=? AND contest_id=?",
            (variant_id, contest_id)
        )
        variant = await cur.fetchone()
        if not variant:
            await message.answer("❌ Bunday variant topilmadi")
            return

    result = await do_vote(message.from_user.id, contest_id, variant_id)
    if result == "ok":
        await message.answer(
            f"✅ Ovozingiz qabul qilindi!\n\n"
            f"🏆 <b>{contest_title}</b>\n"
            f"🗳 Tanlovingiz: <b>{variant[0]}</b>",
            parse_mode="HTML"
        )
    elif result == "already":
        await message.answer("ℹ️ Siz bu tanlovga allaqachon ovoz bergansiz.", parse_mode="HTML")
    elif result == "inactive":
        await message.answer("❌ Bu tanlov tugagan")
    else:
        await message.answer("❌ Xato yuz berdi, qayta urinib ko'ring")


# ================= VOTE — callback =================

@dp.callback_query(F.data.regexp(r"^vv_\d+_\d+$"))
async def vote_variant_cb(call: CallbackQuery):
    try:
        parts = call.data.split("_")
        variant_id = int(parts[1])
        contest_id = int(parts[2])
    except (IndexError, ValueError):
        await call.answer("❌ Xato", show_alert=True)
        return

    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute(
            "SELECT name FROM variants WHERE id=? AND contest_id=?",
            (variant_id, contest_id)
        )
        variant = await cur.fetchone()
        if not variant:
            await call.answer("❌ Variant topilmadi", show_alert=True)
            return

    result = await do_vote(call.from_user.id, contest_id, variant_id)
    if result == "ok":
        await call.answer(f"✅ Ovoz berildi: {variant[0]}", show_alert=True)
        try:
            await call.message.delete()
        except Exception:
            pass
    elif result == "already":
        await call.answer("⚠️ Siz bu tanlovga allaqachon ovoz bergansiz!", show_alert=True)
    elif result == "inactive":
        await call.answer("❌ Bu tanlov tugagan", show_alert=True)
    else:
        await call.answer("❌ Xato yuz berdi", show_alert=True)


# ================= CONTESTS LIST =================

@dp.callback_query(F.data == "list_contests")
async def cb_contests(call: CallbackQuery):
    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute(
            "SELECT id, title FROM contests WHERE active=1 ORDER BY id DESC"
        )
        rows = await cur.fetchall()

    if not rows:
        kb = None
        if call.from_user.id == ADMIN_ID:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin panel", callback_data="admin_panel")]
            ])
        await safe_edit(call, "📋 Hozirda faol tanlovlar yo'q", kb)
        await call.answer()
        return

    buttons = [
        [InlineKeyboardButton(text=r[1], callback_data=f"cinfo_{r[0]}")]
        for r in rows
    ]
    if call.from_user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="🏠 Admin panel", callback_data="admin_panel")])

    await safe_edit(call, "📋 Faol tanlovlar:", InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


# ================= CONTEST INFO =================

@dp.callback_query(F.data.regexp(r"^cinfo_\d+$"))
async def cb_contest_info(call: CallbackQuery):
    try:
        contest_id = int(call.data.split("_")[1])
    except (IndexError, ValueError):
        await call.answer("❌ Xato", show_alert=True)
        return

    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute(
            "SELECT title, active FROM contests WHERE id=?", (contest_id,)
        )
        contest = await cur.fetchone()
        if not contest:
            await call.answer("❌ Topilmadi", show_alert=True)
            return

        cur = await db.execute(
            "SELECT name, votes FROM variants WHERE contest_id=? ORDER BY votes DESC, id",
            (contest_id,)
        )
        variants = await cur.fetchall()

        cur = await db.execute(
            "SELECT COUNT(*) FROM users_votes WHERE contest_id=?", (contest_id,)
        )
        total = (await cur.fetchone())[0]

    title, active = contest
    status = "✅ Faol" if active else "🔴 Tugagan"

    lines = []
    for i, (name, votes) in enumerate(variants, 1):
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        lines.append(f"{medal} <b>{name}</b> — {votes} ovoz")

    text = (
        f"🏆 <b>{title}</b>\n"
        f"📊 {status} | 👥 Jami ovozlar: {total}\n\n"
        + ("\n".join(lines) if lines else "Variantlar yo'q")
    )

    # ======= TUGMALAR =======
    buttons = []
    if call.from_user.id == ADMIN_ID and active:
        buttons.append([
            InlineKeyboardButton(
                text="🔴 Tanlovni yakunlash",
                callback_data=f"end_{contest_id}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Tanlovlar", callback_data="list_contests")])
    if call.from_user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="🏠 Admin panel", callback_data="admin_panel")])

    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


# ================= END CONTEST =================

@dp.callback_query(F.data.regexp(r"^end_\d+$"))
async def cb_end_contest(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("❌ Ruxsat yo'q", show_alert=True)
        return

    try:
        contest_id = int(call.data.split("_")[1])
    except (IndexError, ValueError):
        await call.answer("❌ Xato", show_alert=True)
        return

    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute(
            "SELECT title FROM contests WHERE id=? AND active=1", (contest_id,)
        )
        contest = await cur.fetchone()
        if not contest:
            await call.answer("❌ Topilmadi yoki allaqachon yakunlangan", show_alert=True)
            return

        cur = await db.execute(
            "SELECT name, votes FROM variants WHERE contest_id=? ORDER BY votes DESC",
            (contest_id,)
        )
        variants = await cur.fetchall()

        cur = await db.execute(
            "SELECT COUNT(*) FROM users_votes WHERE contest_id=?", (contest_id,)
        )
        total = (await cur.fetchone())[0]

        await db.execute("UPDATE contests SET active=0 WHERE id=?", (contest_id,))
        await db.commit()

    lines = []
    for i, (name, votes) in enumerate(variants, 1):
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        lines.append(f"{medal} <b>{name}</b>: {votes} ovoz")

    await safe_edit(
        call,
        f"🔴 Tanlov yakunlandi!\n\n"
        f"🏆 <b>{contest[0]}</b>\n"
        f"👥 Jami ovozlar: {total}\n\n"
        f"📊 Natijalar:\n" + "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Tanlovlar", callback_data="list_contests")],
            [InlineKeyboardButton(text="🏠 Admin panel", callback_data="admin_panel")],
        ])
    )
    await call.answer("✅ Yakunlandi")


# ================= MAIN =================

async def main():
    import signal
    
    await db_start()
    logger.info("Bot ishga tushmoqda... ✅")
    
    # Force disconnect any previous webhook/polling sessions
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook cleared ✅")
    except Exception as e:
        logger.warning(f"Webhook clear warning: {e}")
    
    # Wait a bit for Telegram to fully release the connection
    await asyncio.sleep(2)
    
    # Graceful shutdown handler
    loop = asyncio.get_event_loop()
    
    def signal_handler():
        logger.info("Shutdown signal received")
        loop.create_task(shutdown(loop))
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        logger.error(f"Polling failed: {e}")
        raise
    finally:
        await bot.session.close()


async def shutdown(loop):
    """Graceful shutdown"""
    logger.info("Shutting down...")
    await bot.session.close()
    loop.stop()


if __name__ == "__main__":
    import sys, os, signal

    lock_file = "/tmp/samdaqu_bot.lock"
    try:
        import fcntl
        lock_fd = open(lock_file, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
    except (IOError, OSError):
        print("❌ Bot allaqachon ishlayapti! Avval uni to'xtating:")
        print("   pkill -9 -f python && rm -f /tmp/samdaqu_bot.lock")
        sys.exit(1)

    try:
        asyncio.run(main())
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.remove(lock_file)
        except Exception:
            pass

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
# Environment variables'dan oku (güvenli)
TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Eksik değer kontrolü
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable tanımlanmali!")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID environment variable tanımlanmali!")
if not CHANNEL_USERNAME:
    raise ValueError("CHANNEL_USERNAME environment variable tanımlanmali!")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID environment variable tanımlanmali!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ================= FSM STATES =================

class ContestCreate(StatesGroup):
    waiting_title    = State()
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
                message_id INTEGER
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
                variant_id INTEGER NOT NULL
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
        [InlineKeyboardButton(text="📋 Tanlovlar",       callback_data="contests")],
    ])

def user_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Tanlovlar", callback_data="contests")]
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
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode=parse_mode)
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode=parse_mode)


async def do_vote(user_id: int, contest_id: int, variant_id: int) -> str:
    """
    Ovozni bazaga yozadi — limit yo'q, har safar hisoblanadi.
    Qaytaradi: 'ok' | 'inactive' | 'error'
    """
    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute(
            "SELECT active FROM contests WHERE id=?", (contest_id,)
        )
        contest = await cur.fetchone()
        if not contest or not contest[0]:
            return "inactive"

        try:
            await db.execute(
                "INSERT INTO users_votes(user_id, contest_id, variant_id) VALUES(?,?,?)",
                (user_id, contest_id, variant_id)
            )
            await db.execute(
                "UPDATE variants SET votes = votes + 1 WHERE id=?",
                (variant_id,)
            )
            await db.execute(
                "DELETE FROM pending_votes WHERE user_id=?", (user_id,)
            )
            await db.commit()
            return "ok"
        except Exception as e:
            logger.error(f"do_vote xato: {e}")
            return "error"


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
    await show_menu(message)


# ================= CHECK SUB CALLBACK =================

@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(call: CallbackQuery):
    if not await check_sub(call.from_user.id):
        await call.answer("❌ Avval kanalga obuna bo'ling!", show_alert=True)
        return

    await call.message.delete()

    # Pending ovoz bormi?
    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute(
            "SELECT contest_id, variant_id FROM pending_votes WHERE user_id=?",
            (call.from_user.id,)
        )
        pending = await cur.fetchone()

    if pending:
        contest_id, variant_id = pending

        async with aiosqlite.connect("database.db") as db:
            cur = await db.execute(
                "SELECT title FROM contests WHERE id=?", (contest_id,)
            )
            contest = await cur.fetchone()
            cur = await db.execute(
                "SELECT name FROM variants WHERE id=?", (variant_id,)
            )
            variant = await cur.fetchone()

        result = await do_vote(call.from_user.id, contest_id, variant_id)

        if result == "ok":
            await call.message.answer(
                f"✅ Obuna tasdiqlandi va ovozingiz hisoblandi!\n\n"
                f"🏆 <b>{contest[0] if contest else '?'}</b>\n"
                f"🗳 Tanlovingiz: <b>{variant[0] if variant else '?'}</b>",
                parse_mode="HTML",
                reply_markup=user_menu_kb()
            )
            return
        elif result == "inactive":
            await call.message.answer(
                "❌ Afsuski, bu tanlov tugagan.",
                reply_markup=user_menu_kb()
            )
            return

    # Pending yo'q — oddiy menyu
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
    await call.message.answer(
        "📝 Yangi tanlov nomini yuboring:\n\n/bekor — bekor qilish"
    )
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
    await state.update_data(title=title, variants=[], current=0, count=0)
    await state.set_state(ContestCreate.waiting_count)
    await message.answer(
        f"✅ Nom: <b>{title}</b>\n\n"
        f"🔢 Nechchi variant bo'ladi? (cheklov yo'q)\n\n/bekor — bekor qilish",
        parse_mode="HTML"
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
    await message.answer(
        f"1️⃣ 1-variantni yuboring (1/{count})\n\n/bekor — bekor qilish"
    )


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
    count   = data["count"]
    await state.update_data(variants=variants, current=current)

    if current < count:
        await message.answer(
            f"✅ <b>{variant_name}</b> qabul qilindi\n\n"
            f"{current + 1}️⃣ Keyingi variant ({current + 1}/{count})\n\n/bekor — bekor qilish",
            parse_mode="HTML"
        )
    else:
        await state.clear()
        await do_create_contest(message, data["title"], variants)


async def do_create_contest(message: Message, title: str, variants: list):
    bot_info = await bot.get_me()
    try:
        async with aiosqlite.connect("database.db") as db:
            cur = await db.execute(
                "INSERT INTO contests(title) VALUES(?)", (title,)
            )
            contest_id = cur.lastrowid
            variant_rows = []
            for name in variants:
                c = await db.execute(
                    "INSERT INTO variants(contest_id, name) VALUES(?, ?)",
                    (contest_id, name)
                )
                variant_rows.append((c.lastrowid, name))
            await db.commit()

        buttons = [
            [InlineKeyboardButton(
                text=f"🗳 {name}",
                url=f"https://t.me/{bot_info.username}?start=vote_{contest_id}_{vid}"
            )]
            for vid, name in variant_rows
        ]
        variant_list = "\n".join(f"  • {n}" for n in variants)

        post = await bot.send_message(
            CHANNEL_ID,
            f"🏆 <b>{title}</b>\n\nVariantlar:\n{variant_list}\n\n"
            f"👇 Ovoz berish uchun variantni tanlang:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        async with aiosqlite.connect("database.db") as db:
            await db.execute(
                "UPDATE contests SET message_id=? WHERE id=?",
                (post.message_id, contest_id)
            )
            await db.commit()

        await message.answer(
            f"✅ Tanlov yaratildi va kanalga yuborildi!\n\n"
            f"📌 <b>{title}</b>\n🗂 Variantlar: {len(variants)} ta",
            reply_markup=admin_menu_kb(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"do_create_contest xato: {e}")
        await message.answer("❌ Xato yuz berdi, qayta urinib ko'ring.", reply_markup=admin_menu_kb())


# ================= VOTE — deep link =================

@dp.message(CommandStart(deep_link=True))
async def vote_deep(message: Message, state: FSMContext):
    await state.clear()

    try:
        payload    = message.text.split()[1]
        if not payload.startswith("vote_"):
            await show_menu(message)
            return
        parts      = payload.split("_")
        contest_id = int(parts[1])
        variant_id = int(parts[2]) if len(parts) > 2 else None
    except (IndexError, ValueError):
        await show_menu(message)
        return

    # Obuna tekshirish
    if not await check_sub(message.from_user.id):
        if variant_id is not None:
            async with aiosqlite.connect("database.db") as db:
                await db.execute(
                    """INSERT INTO pending_votes(user_id, contest_id, variant_id)
                       VALUES(?, ?, ?)
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

    # Tanlov tekshirish
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

        if variant_id is None:
            cur = await db.execute(
                "SELECT id, name FROM variants WHERE contest_id=? ORDER BY id",
                (contest_id,)
            )
            var_list = await cur.fetchall()
            if not var_list:
                await message.answer("❌ Bu tanlovda variantlar yo'q")
                return
            buttons = [
                [InlineKeyboardButton(
                    text=f"🗳 {name}",
                    callback_data=f"vv_{vid}_{contest_id}"
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
    elif result == "inactive":
        await message.answer("❌ Bu tanlov tugagan")
    else:
        await message.answer("❌ Xato yuz berdi, qayta urinib ko'ring")


# ================= VOTE — callback =================

@dp.callback_query(F.data.startswith("vv_"))
async def vote_variant_cb(call: CallbackQuery):
    try:
        parts      = call.data.split("_")
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
    elif result == "inactive":
        await call.answer("❌ Bu tanlov tugagan", show_alert=True)
    else:
        await call.answer("❌ Xato yuz berdi", show_alert=True)


# ================= CONTESTS LIST =================

@dp.callback_query(F.data == "contests")
async def cb_contests(call: CallbackQuery):
    async with aiosqlite.connect("database.db") as db:
        cur = await db.execute(
            "SELECT id, title FROM contests WHERE active=1 ORDER BY id DESC"
        )
        rows = await cur.fetchall()

    if not rows:
        await call.answer("❌ Hozirda faol tanlovlar yo'q", show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(text=r[1], callback_data=f"contest_{r[0]}")]
        for r in rows
    ]
    await safe_edit(call, "📋 Faol tanlovlar:", InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


# ================= CONTEST INFO =================

@dp.callback_query(F.data.startswith("contest_"))
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

    buttons = []
    if call.from_user.id == ADMIN_ID and active:
        buttons.append([
            InlineKeyboardButton(
                text="🔴 Tanlovni yakunlash",
                callback_data=f"delete_{contest_id}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="contests")])

    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


# ================= DELETE =================

@dp.callback_query(F.data.startswith("delete_"))
async def cb_delete(call: CallbackQuery):
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
        lines.append(f"{medal} {name}: {votes} ovoz")

    await safe_edit(
        call,
        f"🔴 Tanlov yakunlandi!\n\n"
        f"🏆 <b>{contest[0]}</b>\n"
        f"👥 Jami ovozlar: {total}\n\n"
        f"📊 Natijalar:\n" + "\n".join(lines)
    )
    await call.answer("✅ Yakunlandi")


# ================= MAIN =================

async def main():
    await db_start()
    logger.info("Bot ishga tushmoqda... ✅")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

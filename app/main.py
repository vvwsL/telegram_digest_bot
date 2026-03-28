from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import load_config, Config
from .db import Database
from .summarizer import Summarizer, DigestItem
from .keyboards import (
    kb_main, kb_channels, kb_keywords, kb_schedule,
    kb_output, kb_cancel, kb_back,
)


# ─── globals ─────────────────────────────────────────────────────────────────

cfg: Config | None = None
db: Database | None = None
summarizer: Summarizer | None = None
tz: ZoneInfo | None = None
digest_lock = asyncio.Lock()
_fired_slots: set[str] = set()

router = Router()


# ─── FSM states ──────────────────────────────────────────────────────────────

class S(StatesGroup):
    add_channel  = State()
    add_keyword  = State()
    add_schedule = State()
    set_output   = State()


# ─── helpers ─────────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return cfg is not None and user_id in cfg.admin_ids


def _normalize(text: str) -> str:
    return " ".join(text.strip().split())


def _snippet(text: str, max_chars: int) -> str:
    if not text:
        return ""
    for sep in (".", "!", "?"):
        idx = text.find(sep)
        if 0 < idx < max_chars:
            return text[: idx + 1]
    return text[:max_chars]


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.lower().encode()).hexdigest()


def _extract_forwarded_chat(message: Message):
    if message.forward_from_chat:
        return message.forward_from_chat
    origin = getattr(message, "forward_origin", None)
    if origin and hasattr(origin, "chat"):
        return origin.chat
    return None


def _channel_label(ch) -> str:
    if hasattr(ch, "username") and ch.username:
        return f"@{ch.username}"
    if hasattr(ch, "title") and ch.title:
        return ch.title
    return str(getattr(ch, "chat_id", getattr(ch, "id", "?")))


# ─── incoming channel posts ───────────────────────────────────────────────────

@router.channel_post()
async def on_channel_post(message: Message) -> None:
    """Store posts from registered channels as they arrive."""
    assert db is not None and cfg is not None

    ch = await db.get_channel_by_chat_id(message.chat.id)
    if not ch:
        return

    content = message.text or message.caption
    if not content:
        return

    text = _normalize(content)
    if not text:
        return

    snippet = _snippet(text, cfg.snippet_chars)
    msg_hash = _make_hash(text)
    username = message.chat.username
    link = f"https://t.me/{username}/{message.message_id}" if username else None
    ts = int(message.date.replace(tzinfo=timezone.utc).timestamp()) if message.date.tzinfo is None else int(message.date.timestamp())

    await db.add_message(
        channel_id=ch.id,
        chat_id=ch.chat_id,
        message_id=message.message_id,
        ts=ts,
        text=text,
        snippet=snippet,
        link=link,
        msg_hash=msg_hash,
    )


# ─── digest logic ─────────────────────────────────────────────────────────────

async def _run_digest(bot: Bot) -> str:
    assert db is not None and cfg is not None and summarizer is not None and tz is not None

    output_raw = await db.get_setting("output_chat_id")
    if not output_raw:
        return "❌ Не задан output-чат. Настрой через «Куда слать»."

    channels = await db.list_channels()
    if not channels:
        return "❌ Нет каналов для мониторинга."

    now = datetime.now(tz)
    now_ts = int(now.astimezone(timezone.utc).timestamp())

    last_ts_str = await db.get_setting("last_digest_ts")
    since_ts = int(last_ts_str) if last_ts_str else None

    if since_ts:
        messages = await db.fetch_messages_since(since_ts, now_ts)
    else:
        messages = await db.fetch_recent_messages(cfg.history_limit)

    if not messages:
        await db.set_setting("last_digest_ts", str(now_ts))
        return "ℹ️ Нет новых сообщений."

    keywords = [kw.casefold() for _, kw in await db.list_keywords()]
    important_ids = {
        m.id for m in messages
        if keywords and any(kw in m.text.casefold() for kw in keywords)
    } if keywords else set()

    selected = messages[: cfg.max_items]
    items = [
        DigestItem(
            snippet=m.snippet,
            source=f"@{m.channel_username}" if m.channel_username else (m.channel_title or str(m.chat_id)),
            link=m.link,
            important=m.id in important_ids,
        )
        for m in selected
    ]

    start_dt = datetime.fromtimestamp(since_ts, tz=tz) if since_ts else now.replace(hour=0, minute=0, second=0, microsecond=0)
    result = await summarizer.summarize("Дайджест", start_dt, now, items)

    if not result.text:
        return "⚠️ LLM вернул пустой ответ."

    await bot.send_message(int(output_raw), result.text, parse_mode="Markdown")
    await db.create_digest(now_ts, "sent", result.tokens_in, result.tokens_out)
    await db.set_setting("last_digest_ts", str(now_ts))

    return f"✅ Дайджест отправлен ({len(selected)} постов). Токены: {result.tokens_in}/{result.tokens_out}"


async def _digest_tick(bot: Bot) -> None:
    global _fired_slots
    assert db is not None and tz is not None

    now = datetime.now(tz)
    slot = now.strftime("%Y-%m-%d %H:%M")
    if slot in _fired_slots:
        return

    schedule = await db.list_schedule()
    if now.strftime("%H:%M") not in [t for _, t in schedule]:
        return

    _fired_slots.add(slot)
    today = now.strftime("%Y-%m-%d")
    _fired_slots = {s for s in _fired_slots if s.startswith(today)}

    async with digest_lock:
        await _run_digest(bot)


# ─── UI helpers ───────────────────────────────────────────────────────────────

MENU_TEXT = "🤖 *Дайджест-бот*\n\nВыбери раздел:"


async def _show_menu(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(MENU_TEXT, parse_mode="Markdown", reply_markup=kb_main())
    else:
        await target.answer(MENU_TEXT, parse_mode="Markdown", reply_markup=kb_main())


async def _show_channels(target: Message | CallbackQuery) -> None:
    assert db is not None
    channels = await db.list_channels()
    if channels:
        lines = "\n".join(f"• {_channel_label(ch)}" for ch in channels)
        text = (
            f"📢 *Каналы* ({len(channels)}):\n\n{lines}\n\n"
            "_Добавь бота как администратора в каждый канал, чтобы он получал посты._"
        )
    else:
        text = "📢 *Каналы*\n\n_Пока нет ни одного канала._"
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_channels(channels))
    else:
        await target.answer(text, parse_mode="Markdown", reply_markup=kb_channels(channels))


async def _show_keywords(target: Message | CallbackQuery) -> None:
    assert db is not None
    keywords = await db.list_keywords()
    if keywords:
        lines = "\n".join(f"• {kw}" for _, kw in keywords)
        text = f"🔑 *Ключевые слова*:\n\n{lines}\n\n_Если пусто — берутся все посты._"
    else:
        text = "🔑 *Ключевые слова*\n\n_Пока нет. Если не задать — берутся все посты._"
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_keywords(keywords))
    else:
        await target.answer(text, parse_mode="Markdown", reply_markup=kb_keywords(keywords))


async def _show_schedule(target: Message | CallbackQuery) -> None:
    assert db is not None
    times = await db.list_schedule()
    if times:
        lines = "\n".join(f"• {t}" for _, t in times)
        text = f"⏰ *Расписание* дайджестов:\n\n{lines}"
    else:
        text = "⏰ *Расписание*\n\n_Дайджесты не запланированы._"
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_schedule(times))
    else:
        await target.answer(text, parse_mode="Markdown", reply_markup=kb_schedule(times))


# ─── /start & /help ───────────────────────────────────────────────────────────

@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await _show_menu(message, state)


# ─── Callbacks: navigation ────────────────────────────────────────────────────

@router.callback_query(F.data == "main:menu")
async def cb_menu(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await _show_menu(cq, state)


@router.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery) -> None:
    await cq.answer()


# ─── Callbacks: channels ──────────────────────────────────────────────────────

@router.callback_query(F.data == "ch:list")
async def cb_ch_list(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await _show_channels(cq)


@router.callback_query(F.data == "ch:add")
async def cb_ch_add(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await state.set_state(S.add_channel)
    await cq.message.edit_text(
        "📢 *Добавить канал*\n\n"
        "Перешли любой пост из канала\nили введи @username / числовой ID:\n\n"
        "_После добавления не забудь назначить бота администратором канала._",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )


@router.callback_query(F.data.startswith("ch:del:"))
async def cb_ch_del(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    channel_id = int(cq.data.split(":")[2])
    await db.remove_channel(channel_id)
    await cq.answer("🗑️ Удалено")
    await _show_channels(cq)


@router.message(S.add_channel)
async def fsm_add_channel(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    assert db is not None

    chat = _extract_forwarded_chat(message)

    if chat:
        existing = await db.get_channel_by_chat_id(chat.id)
        if existing:
            await state.clear()
            await message.answer(
                f"ℹ️ Канал *{chat.title}* уже добавлен.",
                parse_mode="Markdown",
                reply_markup=kb_back(),
            )
            return
        await db.add_channel(chat_id=chat.id, username=chat.username, title=chat.title)
        await state.clear()
        label = f"@{chat.username}" if chat.username else chat.title
        await message.answer(
            f"✅ Канал *{label}* добавлен!\n\nНазначь бота администратором канала, чтобы он получал посты.",
            parse_mode="Markdown",
            reply_markup=kb_back(),
        )
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Перешли пост из канала или введи @username.", reply_markup=kb_cancel())
        return

    identifier = raw.lstrip("@")
    try:
        chat_obj = await bot.get_chat(int(identifier) if identifier.lstrip("-").isdigit() else f"@{identifier}")
    except Exception:
        await message.answer(
            "❌ Канал не найден. Убедись, что канал публичный, и попробуй ещё раз.",
            reply_markup=kb_cancel(),
        )
        return

    existing = await db.get_channel_by_chat_id(chat_obj.id)
    if existing:
        await state.clear()
        await message.answer("ℹ️ Канал уже добавлен.", reply_markup=kb_back())
        return

    await db.add_channel(chat_id=chat_obj.id, username=chat_obj.username, title=chat_obj.title)
    await state.clear()
    label = f"@{chat_obj.username}" if chat_obj.username else chat_obj.title
    await message.answer(
        f"✅ Канал *{label}* добавлен!\n\nНазначь бота администратором канала, чтобы он получал посты.",
        parse_mode="Markdown",
        reply_markup=kb_back(),
    )


# ─── Callbacks: keywords ──────────────────────────────────────────────────────

@router.callback_query(F.data == "kw:list")
async def cb_kw_list(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await _show_keywords(cq)


@router.callback_query(F.data == "kw:add")
async def cb_kw_add(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await state.set_state(S.add_keyword)
    await cq.message.edit_text(
        "🔑 *Добавить ключевое слово*\n\nВведи слово или фразу:",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )


@router.callback_query(F.data.startswith("kw:del:"))
async def cb_kw_del(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    kw_id = int(cq.data.split(":")[2])
    await db.remove_keyword(kw_id)
    await cq.answer("🗑️ Удалено")
    await _show_keywords(cq)


@router.message(S.add_keyword)
async def fsm_add_keyword(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    assert db is not None
    word = (message.text or "").strip()
    if not word:
        await message.answer("Введи слово или фразу.", reply_markup=kb_cancel())
        return
    added = await db.add_keyword(word)
    await state.clear()
    if added:
        await message.answer(f"✅ Слово «{word}» добавлено.", reply_markup=kb_back())
    else:
        await message.answer(f"ℹ️ Слово «{word}» уже есть.", reply_markup=kb_back())


# ─── Callbacks: schedule ──────────────────────────────────────────────────────

@router.callback_query(F.data == "sch:list")
async def cb_sch_list(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await _show_schedule(cq)


@router.callback_query(F.data == "sch:add")
async def cb_sch_add(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await state.set_state(S.add_schedule)
    await cq.message.edit_text(
        "⏰ *Добавить время дайджеста*\n\nВведи время в формате ЧЧ:ММ, например: `09:00`",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )


@router.callback_query(F.data.startswith("sch:del:"))
async def cb_sch_del(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    sch_id = int(cq.data.split(":")[2])
    await db.remove_schedule_time(sch_id)
    await cq.answer("🗑️ Удалено")
    await _show_schedule(cq)


@router.message(S.add_schedule)
async def fsm_add_schedule(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    assert db is not None
    raw = (message.text or "").strip()
    if not re.match(r"^\d{1,2}:\d{2}$", raw):
        await message.answer("Неверный формат. Введи время как ЧЧ:ММ, например: `18:30`", reply_markup=kb_cancel())
        return
    parts = raw.split(":")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        await message.answer("Неверное время. Часы 0–23, минуты 0–59.", reply_markup=kb_cancel())
        return
    normalized = f"{h:02d}:{m:02d}"
    added = await db.add_schedule_time(normalized)
    await state.clear()
    if added:
        await message.answer(f"✅ Дайджест в *{normalized}* добавлен.", parse_mode="Markdown", reply_markup=kb_back())
    else:
        await message.answer(f"ℹ️ Время *{normalized}* уже есть.", parse_mode="Markdown", reply_markup=kb_back())


# ─── Callbacks: output chat ───────────────────────────────────────────────────

@router.callback_query(F.data == "out:show")
async def cb_out_show(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    current = await db.get_setting("output_chat_id")
    text = f"📤 *Куда слать дайджест*\n\nТекущий chat ID: `{current}`" if current else "📤 *Куда слать дайджест*\n\n_Не задан._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_output(current))


@router.callback_query(F.data == "out:set")
async def cb_out_set(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await state.set_state(S.set_output)
    await cq.message.edit_text(
        "📤 *Задать output-чат*\n\n"
        "Перешли любое сообщение из целевого канала/чата\n"
        "или введи @username / числовой ID чата:",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )


@router.message(S.set_output)
async def fsm_set_output(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    assert db is not None

    chat = _extract_forwarded_chat(message)
    if chat:
        await db.set_setting("output_chat_id", str(chat.id))
        await state.clear()
        label = f"@{chat.username}" if chat.username else chat.title
        await message.answer(f"✅ Output-чат: *{label}*", parse_mode="Markdown", reply_markup=kb_back())
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Перешли сообщение или введи @username / ID.", reply_markup=kb_cancel())
        return

    identifier = raw.lstrip("@")
    if identifier.lstrip("-").isdigit():
        await db.set_setting("output_chat_id", raw if raw.startswith("-") else identifier)
        await state.clear()
        await message.answer(f"✅ Output-чат: `{raw}`", parse_mode="Markdown", reply_markup=kb_back())
        return

    try:
        chat_obj = await bot.get_chat(f"@{identifier}")
    except Exception:
        await message.answer("❌ Чат не найден. Попробуй ещё раз или нажми Отмена.", reply_markup=kb_cancel())
        return

    await db.set_setting("output_chat_id", str(chat_obj.id))
    await state.clear()
    label = f"@{chat_obj.username}" if chat_obj.username else chat_obj.title
    await message.answer(f"✅ Output-чат: *{label}*", parse_mode="Markdown", reply_markup=kb_back())


# ─── Callbacks: digest & status ───────────────────────────────────────────────

@router.callback_query(F.data == "digest:now")
async def cb_digest_now(cq: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer("⏳ Запускаю...")
    await cq.message.edit_text("⏳ Генерирую дайджест...", reply_markup=None)
    async with digest_lock:
        result = await _run_digest(bot)
    await cq.message.edit_text(result, reply_markup=kb_back())


@router.callback_query(F.data == "status:show")
async def cb_status(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()

    channels = await db.list_channels()
    schedule = await db.list_schedule()
    output = await db.get_setting("output_chat_id")
    last_ts = await db.get_setting("last_digest_ts")
    stats = await db.get_token_stats()

    ch_list = "\n".join(f"  • {_channel_label(ch)}" for ch in channels) or "  _нет_"
    sch_list = ", ".join(t for _, t in schedule) or "_нет_"

    if last_ts:
        last_dt = datetime.fromtimestamp(int(last_ts), tz=tz)
        last_str = last_dt.strftime("%d.%m %H:%M")
    else:
        last_str = "_никогда_"

    cost = (stats["total_in"] * 0.15 + stats["total_out"] * 0.60) / 1_000_000

    text = (
        "📊 *Статус*\n\n"
        f"📢 Каналы ({len(channels)}):\n{ch_list}\n\n"
        f"⏰ Расписание: {sch_list}\n"
        f"📤 Output-чат: `{output or 'не задан'}`\n"
        f"🕐 Последний дайджест: {last_str}\n"
        f"🔢 Токены: {stats['total_in']} вх. / {stats['total_out']} исх. (~${cost:.4f})\n"
        f"📨 Всего дайджестов: {stats['count']}"
    )
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_back())


# ─── entry point ─────────────────────────────────────────────────────────────

async def main() -> None:
    global cfg, db, summarizer, tz

    cfg = load_config()
    tz = ZoneInfo(cfg.timezone)
    db = Database(cfg.db_path)
    await db.connect()

    summarizer = Summarizer(api_key=cfg.openai_api_key, model=cfg.openai_model)

    bot = Bot(token=cfg.bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=cfg.timezone)
    scheduler.add_job(_digest_tick, "interval", seconds=60, args=[bot])
    scheduler.start()

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    import asyncio as _asyncio
    _asyncio.run(main())

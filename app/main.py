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
from .db import Database, Folder
from .scraper import fetch_channel_posts
from .summarizer import Summarizer, DigestItem
from .keyboards import (
    kb_main, kb_folders, kb_folder,
    kb_keywords, kb_schedule, kb_output,
    kb_cancel, kb_back, kb_back_to_folders, kb_back_to_folder,
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
    add_folder          = State()
    add_channel_in_folder = State()   # data: folder_id
    add_keyword         = State()
    add_schedule        = State()
    set_output          = State()


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


# ─── digest logic ─────────────────────────────────────────────────────────────

async def _run_folder_digest(bot: Bot, folder: Folder) -> str:
    assert db is not None and cfg is not None and summarizer is not None and tz is not None

    output_raw = await db.get_setting("output_chat_id")
    if not output_raw:
        return "❌ Не задан output-чат."

    channels = await db.list_channels_in_folder(folder.id)
    if not channels:
        return "⚪ Нет каналов."

    now = datetime.now(tz)
    now_ts = int(now.astimezone(timezone.utc).timestamp())

    last_key = f"last_digest_ts:{folder.id}"
    last_ts_str = await db.get_setting(last_key)
    since_ts = int(last_ts_str) if last_ts_str else None

    # always scrape fresh posts before building a digest
    for ch in channels:
        if not ch.username:
            continue
        try:
            posts = await fetch_channel_posts(ch.username, limit=20)
        except Exception:
            continue
        for post in posts:
            snippet = _snippet(post.text, cfg.snippet_chars)
            msg_hash = _make_hash(post.text)
            await db.add_message(
                channel_id=ch.id,
                chat_id=ch.chat_id,
                message_id=post.message_id,
                ts=post.ts,
                text=post.text,
                snippet=snippet,
                link=post.link,
                msg_hash=msg_hash,
            )

    if since_ts:
        messages = await db.fetch_messages_since_by_folder(folder.id, since_ts, now_ts)
    else:
        messages = await db.fetch_recent_messages_by_folder(folder.id, cfg.history_limit)

    if not messages:
        await db.set_setting(last_key, str(now_ts))
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

    start_dt = (
        datetime.fromtimestamp(since_ts, tz=tz)
        if since_ts
        else now.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    result = await summarizer.summarize(folder.name, start_dt, now, items)

    if not result.text:
        return "⚠️ LLM вернул пустой ответ."

    await bot.send_message(int(output_raw), result.text, parse_mode="Markdown")
    await db.create_digest(now_ts, "sent", result.tokens_in, result.tokens_out)
    await db.set_setting(last_key, str(now_ts))

    return f"✅ {len(selected)} постов. Токены: {result.tokens_in}/{result.tokens_out}"


async def _run_all_digests(bot: Bot) -> str:
    assert db is not None

    folders = await db.list_folders()
    if not folders:
        return "❌ Нет папок."

    lines = []
    for folder in folders:
        result = await _run_folder_digest(bot, folder)
        lines.append(f"*{folder.name}*: {result}")

    return "\n".join(lines)


async def _scrape_tick() -> None:
    assert db is not None and cfg is not None

    channels = await db.list_scraper_channels()
    if not channels:
        return

    for ch in channels:
        if not ch.username:
            continue
        try:
            posts = await fetch_channel_posts(ch.username, limit=20)
        except Exception:
            continue

        for post in posts:
            snippet = _snippet(post.text, cfg.snippet_chars)
            msg_hash = _make_hash(post.text)
            await db.add_message(
                channel_id=ch.id,
                chat_id=ch.chat_id,
                message_id=post.message_id,
                ts=post.ts,
                text=post.text,
                snippet=snippet,
                link=post.link,
                msg_hash=msg_hash,
            )


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
        await _run_all_digests(bot)


# ─── UI helpers ───────────────────────────────────────────────────────────────

MENU_TEXT = "🤖 *Дайджест-бот*\n\nВыбери раздел:"


async def _show_menu(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(MENU_TEXT, parse_mode="Markdown", reply_markup=kb_main())
    else:
        await target.answer(MENU_TEXT, parse_mode="Markdown", reply_markup=kb_main())


async def _show_folders(target: Message | CallbackQuery) -> None:
    assert db is not None
    folders = await db.list_folders()
    counts = {}
    for f in folders:
        counts[f.id] = await db.count_channels_in_folder(f.id)

    if folders:
        text = f"📁 *Папки* ({len(folders)}):\n\nНажми на папку, чтобы открыть её."
    else:
        text = "📁 *Папки*\n\n_Пока нет папок. Создай первую!_"

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_folders(folders, counts))
    else:
        await target.answer(text, parse_mode="Markdown", reply_markup=kb_folders(folders, counts))


async def _show_folder(target: Message | CallbackQuery, folder: Folder) -> None:
    assert db is not None
    channels = await db.list_channels_in_folder(folder.id)

    if channels:
        lines = "\n".join(
            f"• @{ch.username}" if ch.username else f"• {ch.title or ch.chat_id}"
            for ch in channels
        )
        text = f"📁 *{folder.name}*\n\n{lines}\n\n_Посты забираются автоматически через t.me/s/_"
    else:
        text = f"📁 *{folder.name}*\n\n_Каналов нет. Добавь первый!_"

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(
            text, parse_mode="Markdown", reply_markup=kb_folder(folder, channels)
        )
    else:
        await target.answer(
            text, parse_mode="Markdown", reply_markup=kb_folder(folder, channels)
        )


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


# ─── Callbacks: folders ───────────────────────────────────────────────────────

@router.callback_query(F.data == "fold:list")
async def cb_fold_list(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await _show_folders(cq)


@router.callback_query(F.data.startswith("fold:view:"))
async def cb_fold_view(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    folder_id = int(cq.data.split(":")[2])
    folder = await db.get_folder(folder_id)
    if not folder:
        await cq.answer("Папка не найдена.", show_alert=True)
        return
    await cq.answer()
    await _show_folder(cq, folder)


@router.callback_query(F.data == "fold:add")
async def cb_fold_add(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await state.set_state(S.add_folder)
    await cq.message.edit_text(
        "📁 *Создать папку*\n\n"
        "Введи название (можно с эмодзи):\n"
        "_Например: 🤖 Нейросети_",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )


@router.message(S.add_folder)
async def fsm_add_folder(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    assert db is not None

    name = _normalize(message.text or "")
    if not name:
        await message.answer("Введи название папки.", reply_markup=kb_cancel())
        return

    folder_id, created = await db.add_folder(name)
    await state.clear()

    if not created:
        await message.answer(
            f"ℹ️ Папка *{name}* уже существует.",
            parse_mode="Markdown",
            reply_markup=kb_back_to_folders(),
        )
        return

    await message.answer(
        f"✅ Папка *{name}* создана!",
        parse_mode="Markdown",
        reply_markup=kb_back_to_folders(),
    )


@router.callback_query(F.data.startswith("fold:del:"))
async def cb_fold_del(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    folder_id = int(cq.data.split(":")[2])
    cnt = await db.count_channels_in_folder(folder_id)
    if cnt > 0:
        await cq.answer(
            f"Нельзя удалить: в папке {cnt} каналов. Удали сначала каналы.",
            show_alert=True,
        )
        return
    await db.remove_folder(folder_id)
    await cq.answer("🗑️ Папка удалена")
    await _show_folders(cq)


@router.callback_query(F.data.startswith("fold:digest:"))
async def cb_fold_digest(cq: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    folder_id = int(cq.data.split(":")[2])
    folder = await db.get_folder(folder_id)
    if not folder:
        await cq.answer("Папка не найдена.", show_alert=True)
        return

    await cq.answer("⏳ Запускаю...")
    await cq.message.edit_text(
        f"⏳ Генерирую дайджест папки *{folder.name}*...",
        parse_mode="Markdown",
        reply_markup=None,
    )
    async with digest_lock:
        result = await _run_folder_digest(bot, folder)

    await cq.message.edit_text(
        f"*{folder.name}*\n\n{result}",
        parse_mode="Markdown",
        reply_markup=kb_back_to_folder(folder_id),
    )


# ─── Callbacks: channels in folder ────────────────────────────────────────────

@router.callback_query(F.data.startswith("ch:add_in:"))
async def cb_ch_add_in(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    folder_id = int(cq.data.split(":")[2])
    await cq.answer()
    await state.set_state(S.add_channel_in_folder)
    await state.update_data(folder_id=folder_id)
    await cq.message.edit_text(
        "🌐 *Добавить каналы*\n\n"
        "• Отправь @username (один или список)\n"
        "• Или перешли пост из публичного канала\n\n"
        "_Посты забираются через t.me/s/ — добавлять бота в канал не нужно._",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )


async def _add_single_channel(username: str, folder_id: int) -> str:
    """Try to add one channel, return status line."""
    assert db is not None
    try:
        posts = await fetch_channel_posts(username, limit=1)
    except Exception as exc:
        return f"❌ @{username} — {exc}"

    channel_id, created = await db.add_scraper_channel(username, folder_id=folder_id)
    if created:
        return f"✅ @{username} добавлен"
    return f"ℹ️ @{username} уже есть"


@router.message(S.add_channel_in_folder)
async def fsm_add_channel_in_folder(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    assert db is not None

    data = await state.get_data()
    folder_id: int = data.get("folder_id", -1)

    # ── forwarded post from a channel ────────────────────────────────────────
    if message.forward_from_chat and message.forward_from_chat.username:
        username = message.forward_from_chat.username
        await message.answer("⏳ Проверяю канал...")
        result = await _add_single_channel(username, folder_id)
        await state.clear()
        await message.answer(result, parse_mode="Markdown", reply_markup=kb_back_to_folder(folder_id))
        return

    # ── parse usernames from text ────────────────────────────────────────────
    text = message.text or ""
    usernames = re.findall(r"@?([a-zA-Z0-9_]{3,})", text)

    if not usernames:
        await message.answer(
            "Введи @username, список или перешли пост из канала.",
            reply_markup=kb_cancel(),
        )
        return

    # single channel — quick path
    if len(usernames) == 1:
        await message.answer("⏳ Проверяю канал...")
        result = await _add_single_channel(usernames[0], folder_id)
        await state.clear()
        await message.answer(result, parse_mode="Markdown", reply_markup=kb_back_to_folder(folder_id))
        return

    # bulk add
    await message.answer(f"⏳ Добавляю {len(usernames)} каналов...")
    results = []
    for uname in usernames:
        results.append(await _add_single_channel(uname, folder_id))

    await state.clear()
    await message.answer(
        "\n".join(results),
        parse_mode="Markdown",
        reply_markup=kb_back_to_folder(folder_id),
    )


@router.callback_query(F.data.startswith("ch:del:"))
async def cb_ch_del(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    channel_id = int(cq.data.split(":")[2])

    # get folder_id before deleting
    channels = await db.list_all_channels()
    ch = next((c for c in channels if c.id == channel_id), None)
    folder_id = ch.folder_id if ch else None

    await db.remove_channel(channel_id)
    await cq.answer("🗑️ Удалено")

    if folder_id:
        folder = await db.get_folder(folder_id)
        if folder:
            await _show_folder(cq, folder)
            return
    await _show_folders(cq)


# ─── Callbacks: keywords ──────────────────────────────────────────────────────

@router.callback_query(F.data == "kw:list")
async def cb_kw_list(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    keywords = await db.list_keywords()
    if keywords:
        lines = "\n".join(f"• {kw}" for _, kw in keywords)
        text = f"🔑 *Ключевые слова*:\n\n{lines}\n\n_Если пусто — берутся все посты._"
    else:
        text = "🔑 *Ключевые слова*\n\n_Пока нет. Если не задать — берутся все посты._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_keywords(keywords))


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
    keywords = await db.list_keywords()
    if keywords:
        lines = "\n".join(f"• {kw}" for _, kw in keywords)
        text = f"🔑 *Ключевые слова*:\n\n{lines}"
    else:
        text = "🔑 *Ключевые слова*\n\n_Пока нет._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_keywords(keywords))


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
    assert db is not None
    await cq.answer()
    times = await db.list_schedule()
    if times:
        lines = "\n".join(f"• {t}" for _, t in times)
        text = f"⏰ *Расписание* дайджестов:\n\n{lines}"
    else:
        text = "⏰ *Расписание*\n\n_Дайджесты не запланированы._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_schedule(times))


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
    times = await db.list_schedule()
    if times:
        lines = "\n".join(f"• {t}" for _, t in times)
        text = f"⏰ *Расписание*:\n\n{lines}"
    else:
        text = "⏰ *Расписание*\n\n_Нет._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_schedule(times))


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
        "Введи @username или числовой ID чата:",
        parse_mode="Markdown",
        reply_markup=kb_cancel(),
    )


@router.message(S.set_output)
async def fsm_set_output(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    assert db is not None

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Введи @username или числовой ID.", reply_markup=kb_cancel())
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
    await cq.message.edit_text("⏳ Генерирую дайджесты всех папок...", reply_markup=None)
    async with digest_lock:
        result = await _run_all_digests(bot)
    await cq.message.edit_text(result, parse_mode="Markdown", reply_markup=kb_back())


@router.callback_query(F.data == "status:show")
async def cb_status(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()

    folders = await db.list_folders()
    schedule = await db.list_schedule()
    output = await db.get_setting("output_chat_id")
    stats = await db.get_token_stats()

    folder_lines = []
    for f in folders:
        cnt = await db.count_channels_in_folder(f.id)
        folder_lines.append(f"  {f.name} ({cnt} каналов)")
    folders_str = "\n".join(folder_lines) or "  _нет_"

    sch_list = ", ".join(t for _, t in schedule) or "_нет_"
    cost = (stats["total_in"] * 0.15 + stats["total_out"] * 0.60) / 1_000_000

    text = (
        "📊 *Статус*\n\n"
        f"📁 Папки ({len(folders)}):\n{folders_str}\n\n"
        f"⏰ Расписание: {sch_list}\n"
        f"📤 Output-чат: `{output or 'не задан'}`\n"
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
    scheduler.add_job(_scrape_tick, "interval", seconds=cfg.scrape_interval)
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

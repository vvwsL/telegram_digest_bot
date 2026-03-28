from __future__ import annotations

import asyncio
import shlex
from datetime import datetime, timezone, timedelta, time as dtime
import os
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import load_config, Config
from .db import Database, Topic, Source, MessageRow
from .fetcher import HistoryFetcher
from .summarizer import Summarizer, DigestItem
from .windows import parse_days, parse_time_range, window_end_for_now, format_days
from .keyboards import (
    kb_main_menu, kb_topics, kb_topic_detail, kb_topic_del_confirm,
    kb_sources, kb_keywords, kb_windows, kb_cancel, kb_back, kb_status_detail,
)


# ─── globals ────────────────────────────────────────────────────────────────

cfg: Config | None = None
db: Database | None = None
summarizer: Summarizer | None = None
fetcher: HistoryFetcher | None = None
tz: ZoneInfo | None = None
digest_lock = asyncio.Lock()

router = Router()


# ─── FSM states ─────────────────────────────────────────────────────────────

class S(StatesGroup):
    topic_add_name    = State()
    topic_rename_new  = State()   # data: topic_id, topic_name
    src_add_source    = State()   # data: topic_id, topic_name
    kw_add_keyword    = State()   # data: topic_id, topic_name
    window_add_days   = State()
    window_add_time   = State()   # data: days_str


# ─── helpers ────────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return cfg is not None and user_id in cfg.admin_ids


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().split())


def _parse_time_str(raw: str) -> dtime:
    h, m = raw.split(":")
    return dtime(hour=int(h), minute=int(m))


def _extract_forwarded_chat(message: Message):
    if message.forward_from_chat:
        return message.forward_from_chat
    origin = getattr(message, "forward_origin", None)
    if origin and hasattr(origin, "chat"):
        return origin.chat
    return None


async def _upsert_source_from_chat(chat) -> Source:
    assert db is not None
    await db.add_source(chat_id=chat.id, username=chat.username, title=chat.title)
    source = await db.get_source_by_chat_id(chat.id)
    if source is None:
        raise RuntimeError("Failed to create source")
    return source




# ─── digest building ─────────────────────────────────────────────────────────

async def _build_digest_items(
    topic: Topic, start_ts: int, end_ts: int, first_run: bool
) -> list[DigestItem]:
    assert db is not None and cfg is not None
    if first_run:
        messages = await db.fetch_recent_messages_for_topic(topic.id, limit=30)
    else:
        messages = await db.fetch_messages_for_topic_window(topic.id, start_ts, end_ts)

    keywords = [kw.casefold() for kw in await db.list_keywords(topic.id)]
    important: list[MessageRow] = []
    normal: list[MessageRow] = []
    for msg in messages:
        if keywords and any(kw in msg.text.casefold() for kw in keywords):
            important.append(msg)
        else:
            normal.append(msg)

    seen: set[int] = set()
    selected: list[MessageRow] = []
    for msg in important:
        if msg.id not in seen:
            selected.append(msg)
            seen.add(msg.id)
    for msg in normal[: cfg.max_items_per_topic]:
        if msg.id not in seen:
            selected.append(msg)
            seen.add(msg.id)

    important_ids = {m.id for m in important}
    items: list[DigestItem] = []
    for msg in selected:
        src = f"@{msg.source_username}" if msg.source_username else (msg.source_title or str(msg.chat_id))
        items.append(DigestItem(snippet=msg.snippet, source=src, link=msg.link, important=msg.id in important_ids))
    return items


async def _digest_tick(bot: Bot) -> None:
    assert db is not None and tz is not None and cfg is not None and summarizer is not None and fetcher is not None
    async with digest_lock:
        output_raw = await db.get_setting("output_chat_id")
        if not output_raw:
            return
        output_chat_id = int(output_raw)
        topics = await db.list_topics()
        if not topics:
            return
        windows = await db.list_windows()
        if not windows:
            return

        now = datetime.now(tz)
        for row in windows:
            days = [int(x) for x in row.days.split(",") if x]
            start = _parse_time_str(row.start)
            end = _parse_time_str(row.end)
            window_range = window_end_for_now(now, days, start, end, tz)
            if not window_range:
                continue
            start_dt, end_dt = window_range
            start_ts = int(start_dt.astimezone(timezone.utc).timestamp())
            end_ts = int(end_dt.astimezone(timezone.utc).timestamp())

            for topic in topics:
                created = await db.create_digest(topic.id, row.id, end_ts)
                if not created:
                    continue
                try:
                    first_run = await db.is_first_digest(topic.id)
                    if first_run:
                        # Silent collection only — no summary on first run
                        await fetcher.fetch_all_sources_for_digest(
                            db, topic.id,
                            since_ts=None,
                            until_ts=end_ts,
                            first_run=True,
                            history_limit=cfg.history_limit,
                        )
                        await db.finish_digest(topic.id, row.id, end_ts, "seeded", None)
                        continue
                    # Regular digest: pull messages for this window period
                    await fetcher.fetch_all_sources_for_digest(
                        db, topic.id,
                        since_ts=start_ts,
                        until_ts=end_ts,
                        first_run=False,
                        history_limit=cfg.history_limit,
                    )
                    items = await _build_digest_items(topic, start_ts, end_ts, first_run=False)
                    if not items:
                        await db.finish_digest(topic.id, row.id, end_ts, "empty", None)
                        continue
                    result = await summarizer.summarize(topic.name, start_dt, end_dt, items)
                    if result.text:
                        token_line = (
                            f"\n\n_🔢 Токены: {result.tokens_in} вх. / {result.tokens_out} исх._"
                            if result.tokens_total > 0 else ""
                        )
                        await bot.send_message(
                            output_chat_id,
                            result.text + token_line,
                            parse_mode="Markdown",
                        )
                        await db.finish_digest(
                            topic.id, row.id, end_ts, "sent", None,
                            tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                        )
                    else:
                        await db.finish_digest(topic.id, row.id, end_ts, "empty", "LLM returned empty")
                except Exception as exc:
                    await db.finish_digest(topic.id, row.id, end_ts, "error", str(exc))


# ─── emergency digest ────────────────────────────────────────────────────────

async def _run_emergency_digest(output_chat_id: int, bot: Bot) -> str:
    """Fetch last 30 posts per source right now, summarize per topic, send to output."""
    assert db is not None and cfg is not None and summarizer is not None and fetcher is not None and tz is not None
    topics = await db.list_topics()
    if not topics:
        return "❌ Нет ни одной темы."
    results: list[str] = []
    now = datetime.now(tz)
    for topic in topics:
        # Pull fresh data
        await fetcher.fetch_all_sources_for_digest(
            db, topic.id,
            since_ts=None,
            until_ts=int(now.astimezone(timezone.utc).timestamp()),
            first_run=True,
            history_limit=cfg.history_limit,
        )
        messages = await db.fetch_recent_messages_for_topic(topic.id, limit=cfg.history_limit)
        if not messages:
            continue
        keywords = [kw.casefold() for kw in await db.list_keywords(topic.id)]
        important_ids = {
            m.id for m in messages
            if keywords and any(kw in m.text.casefold() for kw in keywords)
        }
        items = [
            DigestItem(
                snippet=m.snippet,
                source=f"@{m.source_username}" if m.source_username else (m.source_title or str(m.chat_id)),
                link=m.link,
                important=m.id in important_ids,
            )
            for m in messages
        ]
        fake_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        result = await summarizer.summarize(topic.name, fake_start, now, items)
        if result.text:
            token_line = (
                f"\n\n_🔢 Токены: {result.tokens_in} вх. / {result.tokens_out} исх._"
                if result.tokens_total > 0 else ""
            )
            await bot.send_message(output_chat_id, result.text + token_line, parse_mode="Markdown")
            await db.log_emergency_tokens(topic.id, result.tokens_in, result.tokens_out)
            results.append(f"✅ {topic.name}")
        else:
            results.append(f"⚠️ {topic.name} — LLM вернул пустой ответ")
    return "🚨 *Экстренная сводка отправлена:*\n" + "\n".join(results) if results else "❌ Нет данных."


# ─── UI helpers ──────────────────────────────────────────────────────────────

MAIN_MENU_TEXT = (
    "🤖 *Дайджест-бот*\n\n"
    "Выбери раздел:"
)


async def _show_main_menu(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(MAIN_MENU_TEXT, parse_mode="Markdown", reply_markup=kb_main_menu())
    else:
        await target.answer(MAIN_MENU_TEXT, parse_mode="Markdown", reply_markup=kb_main_menu())


async def _show_topics(target: Message | CallbackQuery) -> None:
    assert db is not None
    topics = await db.list_topics()
    text = "📋 *Темы*\n\nВыбери тему или добавь новую:" if topics else "📋 *Темы*\n\nПока нет ни одной темы."
    markup = kb_topics(topics)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await target.answer(text, parse_mode="Markdown", reply_markup=markup)


async def _show_topic(cq: CallbackQuery, topic: Topic) -> None:
    assert db is not None
    sources = await db.list_topic_sources(topic.id)
    keywords = await db.list_keywords(topic.id)
    src_list = ", ".join(
        (f"@{s.username}" if s.username else s.title or str(s.chat_id)) for s in sources
    ) or "нет"
    kw_list = ", ".join(keywords) or "нет"
    text = (
        f"📌 *{topic.name}*\n\n"
        f"📡 Источники: {src_list}\n"
        f"🔑 Ключевые слова: {kw_list}"
    )
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_topic_detail(topic))


# ─── /start & /help ──────────────────────────────────────────────────────────

@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await _show_main_menu(message, state)


# ─── Callback: navigation ─────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:main")
async def cb_menu_main(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔ Нет доступа.", show_alert=True)
        return
    await cq.answer()
    await _show_main_menu(cq, state)


@router.callback_query(F.data == "menu:topics")
async def cb_menu_topics(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await _show_topics(cq)


@router.callback_query(F.data == "menu:windows")
async def cb_menu_windows(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    windows = await db.list_windows()
    text = (
        "⏰ *Расписание дайджестов*\n\n"
        "Дайджест отправляется в конец каждого окна.\n"
        "Пример: `пн-вс 00:00-06:00` → дайджест в 06:00 каждый день.\n\n"
        "Быстрые варианты:\n"
        "`/window_add пн-вс 18:00-06:00` — дайджест в 06:00\n"
        "`/window_add пн-вс 06:00-18:00` — дайджест в 18:00"
    )
    if not windows:
        text += "\n\n_Окон пока нет._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_windows(windows))


# ─── Callback: topic actions ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("topic:show:"))
async def cb_topic_show(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    topic_id = int(cq.data.split(":")[2])
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await cq.message.edit_text("❌ Тема не найдена.", reply_markup=kb_back("menu:topics"))
        return
    await _show_topic(cq, row)


@router.callback_query(F.data.startswith("topic:del_confirm:"))
async def cb_topic_del_confirm(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    topic_id = int(cq.data.split(":")[2])
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await cq.message.edit_text("❌ Тема не найдена.", reply_markup=kb_back("menu:topics"))
        return
    await cq.message.edit_text(
        f"🗑️ Удалить тему *{row.name}*?\n\nЭто также удалит все привязанные источники, ключевые слова и дайджесты.",
        parse_mode="Markdown",
        reply_markup=kb_topic_del_confirm(row),
    )


@router.callback_query(F.data.startswith("topic:del:"))
async def cb_topic_del(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    topic_id = int(cq.data.split(":")[2])
    row = await db.get_topic_by_id(topic_id)
    name = row.name if row else str(topic_id)
    await db.remove_topic_by_id(topic_id)
    topics = await db.list_topics()
    text = f"✅ Тема *{name}* удалена.\n\n📋 *Темы*\n\nВыбери тему или добавь новую:" if topics else f"✅ Тема *{name}* удалена.\n\n📋 *Темы*\n\nПока нет ни одной темы."
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_topics(topics))


@router.callback_query(F.data.startswith("topic:rename:"))
async def cb_topic_rename(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    topic_id = int(cq.data.split(":")[2])
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await cq.message.edit_text("❌ Тема не найдена.", reply_markup=kb_back("menu:topics"))
        return
    await state.set_state(S.topic_rename_new)
    await state.update_data(topic_id=topic_id, topic_name=row.name)
    await cq.message.edit_text(
        f"✏️ Переименовать *{row.name}*\n\nВведи новое название:",
        parse_mode="Markdown",
        reply_markup=kb_cancel(f"topic:show:{topic_id}"),
    )


@router.callback_query(F.data.startswith("topic:sources:"))
async def cb_topic_sources(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    topic_id = int(cq.data.split(":")[2])
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await cq.message.edit_text("❌ Тема не найдена.", reply_markup=kb_back("menu:topics"))
        return
    sources = await db.list_topic_sources(topic_id)
    text = f"📡 *Источники темы «{row.name}»*\n\n"
    if sources:
        text += "\n".join(
            f"• {('@' + s.username) if s.username else (s.title or str(s.chat_id))}"
            for s in sources
        )
    else:
        text += "_Источников пока нет._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_sources(row, sources))


@router.callback_query(F.data.startswith("topic:src_add:"))
async def cb_src_add_start(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    topic_id = int(cq.data.split(":")[2])
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await cq.message.edit_text("❌ Тема не найдена.", reply_markup=kb_back("menu:topics"))
        return
    await state.set_state(S.src_add_source)
    await state.update_data(topic_id=topic_id, topic_name=row.name)
    await cq.message.edit_text(
        f"📡 *Добавить источник в «{row.name}»*\n\n"
        "Отправь @username канала, его числовой ID\nили перешли любое сообщение из него:",
        parse_mode="Markdown",
        reply_markup=kb_cancel(f"topic:sources:{topic_id}"),
    )


@router.callback_query(F.data.startswith("topic:keywords:"))
async def cb_topic_keywords(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    topic_id = int(cq.data.split(":")[2])
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await cq.message.edit_text("❌ Тема не найдена.", reply_markup=kb_back("menu:topics"))
        return
    keywords = await db.list_keywords(topic_id)
    text = f"🔑 *Ключевые слова темы «{row.name}»*\n\n"
    text += ("\n".join(f"• {kw}" for kw in keywords)) if keywords else "_Ключевых слов пока нет._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_keywords(row, keywords))


@router.callback_query(F.data.startswith("topic:kw_add:"))
async def cb_kw_add_start(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    topic_id = int(cq.data.split(":")[2])
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await cq.message.edit_text("❌ Тема не найдена.", reply_markup=kb_back("menu:topics"))
        return
    await state.set_state(S.kw_add_keyword)
    await state.update_data(topic_id=topic_id, topic_name=row.name)
    await cq.message.edit_text(
        f"🔑 *Добавить ключевое слово в «{row.name}»*\n\nВведи слово или фразу:",
        parse_mode="Markdown",
        reply_markup=kb_cancel(f"topic:keywords:{topic_id}"),
    )


@router.callback_query(F.data.startswith("src:del:"))
async def cb_src_del(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer("🗑️ Удалено")
    parts = cq.data.split(":")
    topic_id, source_id = int(parts[2]), int(parts[3])
    await db.unlink_topic_source(topic_id, source_id)
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await _show_topics(cq)
        return
    sources = await db.list_topic_sources(topic_id)
    text = f"📡 *Источники темы «{row.name}»*\n\n"
    text += "\n".join(
        f"• {('@' + s.username) if s.username else (s.title or str(s.chat_id))}" for s in sources
    ) if sources else "_Источников пока нет._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_sources(row, sources))


@router.callback_query(F.data.startswith("kw:del:"))
async def cb_kw_del(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    parts = cq.data.split(":")
    topic_id = int(parts[2])
    kw_prefix = parts[3]
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await cq.answer()
        return
    # Find full keyword by prefix (handles truncation in callback data)
    all_kws = await db.list_keywords(topic_id)
    target = next((k for k in all_kws if k.startswith(kw_prefix) or k[:30] == kw_prefix), None)
    if target:
        await db.remove_keyword(topic_id, target)
        await cq.answer(f"🗑️ «{target}» удалено")
    else:
        await cq.answer("Не найдено")
    keywords = await db.list_keywords(topic_id)
    text = f"🔑 *Ключевые слова темы «{row.name}»*\n\n"
    text += ("\n".join(f"• {kw}" for kw in keywords)) if keywords else "_Ключевых слов пока нет._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_keywords(row, keywords))


@router.callback_query(F.data.startswith("window:del:"))
async def cb_window_del(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer("🗑️ Удалено")
    window_id = int(cq.data.split(":")[2])
    await db.remove_window(window_id)
    windows = await db.list_windows()
    text = "⏰ *Расписание дайджестов*\n\n"
    if not windows:
        text += "_Окон пока нет._"
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_windows(windows))


# ─── Callback: actions ───────────────────────────────────────────────────────

@router.callback_query(F.data == "action:setoutput")
async def cb_setoutput(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await db.set_setting("output_chat_id", str(cq.message.chat.id))
    await cq.answer("✅ Чат установлен", show_alert=True)


@router.callback_query(F.data == "action:status")
async def cb_status(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    topics = await db.list_topics()
    windows = await db.list_windows()
    output = await db.get_setting("output_chat_id")
    lines = [
        "📊 *Статус*\n",
        f"📌 Тем: *{len(topics)}*",
        f"⏰ Окон: *{len(windows)}*",
        f"📤 Output-чат: *{output or 'не задан'}*",
    ]
    if topics:
        lines.append("\n*Темы:*")
        for t in topics:
            srcs = await db.list_topic_sources(t.id)
            kws = await db.list_keywords(t.id)
            lines.append(f"  • {t.name} — {len(srcs)} ист., {len(kws)} ключей")
    await cq.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_status_detail())


@router.callback_query(F.data == "action:token_stats")
async def cb_token_stats(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    assert db is not None
    await cq.answer()
    stats = await db.get_token_stats()
    total_in = stats["total_in"]
    total_out = stats["total_out"]
    total = total_in + total_out

    # Approximate cost for gpt-4o-mini: $0.15/1M in, $0.60/1M out
    cost_usd = (total_in * 0.15 + total_out * 0.60) / 1_000_000

    lines = [
        "🔢 *Статистика токенов*\n",
        f"Входящих (промпт): *{total_in:,}*",
        f"Исходящих (ответ): *{total_out:,}*",
        f"Всего: *{total:,}*",
        f"≈ Стоимость (gpt-4o-mini): *${cost_usd:.4f}*",
    ]
    if stats["per_topic"]:
        lines.append("\n*По темам:*")
        for t in stats["per_topic"]:
            t_total = t["tokens_in"] + t["tokens_out"]
            lines.append(f"  • {t['name']}: {t_total:,} токенов ({t['count']} дайджестов)")

    lines.append("\n_Стоимость приблизительная. Точные данные — в дашборде OpenAI._")
    await cq.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_back("action:status"))


@router.callback_query(F.data == "action:emergency_digest")
async def cb_emergency_digest(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔ Нет доступа.", show_alert=True)
        return
    assert db is not None
    output_raw = await db.get_setting("output_chat_id")
    if not output_raw:
        await cq.answer("❌ Сначала задай output-чат через 📤", show_alert=True)
        return
    await cq.answer("⏳ Собираю данные…", show_alert=True)
    await cq.message.edit_text(
        "⏳ *Экстренная сводка*\n\nЗабираю последние 30 постов из каналов…",
        parse_mode="Markdown",
    )
    summary = await _run_emergency_digest(int(output_raw), cq.bot)
    await cq.message.edit_text(summary, parse_mode="Markdown", reply_markup=kb_back("menu:main"))


@router.callback_query(F.data == "action:add_topic")
async def cb_add_topic_start(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await state.set_state(S.topic_add_name)
    await cq.message.edit_text(
        "➕ *Новая тема*\n\nВведи название темы:",
        parse_mode="Markdown",
        reply_markup=kb_cancel("menu:topics"),
    )


@router.callback_query(F.data == "action:add_window")
async def cb_add_window_start(cq: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cq.from_user.id):
        await cq.answer()
        return
    await cq.answer()
    await state.set_state(S.window_add_days)
    await cq.message.edit_text(
        "⏰ *Новое окно*\n\n"
        "Введи дни недели:\n"
        "• `пн-пт` — будни\n"
        "• `пн-вс` или `*` — каждый день\n"
        "• `пн,ср,пт` — конкретные дни",
        parse_mode="Markdown",
        reply_markup=kb_cancel("menu:windows"),
    )


@router.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery) -> None:
    await cq.answer()


# ─── FSM: text input handlers ────────────────────────────────────────────────

@router.message(S.topic_add_name)
async def fsm_topic_add_name(message: Message, state: FSMContext) -> None:
    assert db is not None
    name = _normalize_text(message.text or "")
    if not name:
        await message.answer("⚠️ Введи непустое название.")
        return
    topic_id = await db.add_topic(name)
    await state.clear()
    if topic_id is None:
        await message.answer(f"⚠️ Тема *{name}* уже существует.", parse_mode="Markdown",
                             reply_markup=kb_back("menu:topics"))
    else:
        topics = await db.list_topics()
        await message.answer(
            f"✅ Тема *{name}* добавлена.\n\n📋 *Темы*:",
            parse_mode="Markdown",
            reply_markup=kb_topics(topics),
        )


@router.message(S.topic_rename_new)
async def fsm_topic_rename_new(message: Message, state: FSMContext) -> None:
    assert db is not None
    data = await state.get_data()
    topic_id = data["topic_id"]
    old_name = data["topic_name"]
    new_name = _normalize_text(message.text or "")
    if not new_name:
        await message.answer("⚠️ Введи непустое название.")
        return
    await db.rename_topic_by_id(topic_id, new_name)
    await state.clear()
    row = await db.get_topic_by_id(topic_id)
    if row:
        await message.answer(
            f"✅ Тема переименована: *{old_name}* → *{new_name}*",
            parse_mode="Markdown",
            reply_markup=kb_topic_detail(row),
        )
    else:
        await _show_topics(message)


@router.message(S.src_add_source)
async def fsm_src_add_source(message: Message, state: FSMContext) -> None:
    assert db is not None and fetcher is not None
    data = await state.get_data()
    topic_id = data["topic_id"]
    topic_name = data["topic_name"]

    source: Source | None = None

    # Try forwarded message first (works for private channels too)
    fwd_chat = _extract_forwarded_chat(message)
    if fwd_chat:
        source = await _upsert_source_from_chat(fwd_chat)
    else:
        arg = _normalize_text(message.text or "")
        if not arg:
            await message.answer("⚠️ Введи @username или перешли сообщение.")
            return
        # Try existing DB record first
        cleaned = arg.lstrip("@")
        if cleaned.lstrip("-").isdigit():
            source = await db.get_source_by_chat_id(int(cleaned))
        if not source:
            source = await db.get_source_by_username(cleaned)
        if not source:
            # Resolve via Pyrogram (MTProto) — works for public channels without bot being a member
            info = await fetcher.resolve_channel(arg)
            if info:
                await db.add_source(chat_id=info.chat_id, username=info.username, title=info.title)
                source = await db.get_source_by_chat_id(info.chat_id)

    if not source:
        await message.answer(
            "❌ Не удалось найти канал.\n\n"
            "Для *публичного* канала достаточно @username.\n"
            "Для *приватного* — перешли любое сообщение из него.",
            parse_mode="Markdown",
            reply_markup=kb_cancel(f"topic:sources:{topic_id}"),
        )
        return

    row = await db.get_topic_by_id(topic_id)
    if not row:
        await state.clear()
        await _show_topics(message)
        return

    linked = await db.link_topic_source(topic_id, source.id)
    await state.clear()
    src_label = f"@{source.username}" if source.username else (source.title or str(source.chat_id))
    result = f"✅ *{src_label}* добавлен" if linked else f"ℹ️ *{src_label}* уже привязан"
    sources = await db.list_topic_sources(topic_id)
    text = f"{result} к теме *{topic_name}*\n\n📡 *Источники:*\n"
    text += "\n".join(
        f"• {('@' + s.username) if s.username else (s.title or str(s.chat_id))}" for s in sources
    ) if sources else "_нет_"
    await message.answer(text, parse_mode="Markdown", reply_markup=kb_sources(row, sources))


@router.message(S.kw_add_keyword)
async def fsm_kw_add_keyword(message: Message, state: FSMContext) -> None:
    assert db is not None
    data = await state.get_data()
    topic_id = data["topic_id"]
    topic_name = data["topic_name"]
    keyword = _normalize_text(message.text or "")
    if not keyword:
        await message.answer("⚠️ Введи непустое ключевое слово.")
        return
    row = await db.get_topic_by_id(topic_id)
    if not row:
        await state.clear()
        await _show_topics(message)
        return
    ok = await db.add_keyword(topic_id, keyword)
    await state.clear()
    result = f"✅ *{keyword}* добавлено" if ok else f"ℹ️ *{keyword}* уже есть"
    keywords = await db.list_keywords(topic_id)
    text = f"{result} в теме *{topic_name}*\n\n🔑 *Ключевые слова:*\n"
    text += ("\n".join(f"• {kw}" for kw in keywords)) if keywords else "_нет_"
    await message.answer(text, parse_mode="Markdown", reply_markup=kb_keywords(row, keywords))


@router.message(S.window_add_days)
async def fsm_window_add_days(message: Message, state: FSMContext) -> None:
    raw = _normalize_text(message.text or "")
    try:
        days = parse_days(raw)
    except ValueError as e:
        await message.answer(f"⚠️ {e}\n\nПопробуй снова (например: `пн-вс`):", parse_mode="Markdown")
        return
    await state.set_state(S.window_add_time)
    await state.update_data(days_str=",".join(str(d) for d in days))
    await message.answer(
        "⏰ Теперь введи временной диапазон в формате `ЧЧ:ММ-ЧЧ:ММ`\n\n"
        "Дайджест отправляется в *конец* диапазона.\n"
        "Примеры:\n"
        "• `00:00-06:00` → дайджест в 06:00\n"
        "• `06:00-18:00` → дайджест в 18:00",
        parse_mode="Markdown",
        reply_markup=kb_cancel("menu:windows"),
    )


@router.message(S.window_add_time)
async def fsm_window_add_time(message: Message, state: FSMContext) -> None:
    assert db is not None
    data = await state.get_data()
    days_str = data["days_str"]
    raw = _normalize_text(message.text or "")
    try:
        start, end = parse_time_range(raw)
    except ValueError as e:
        await message.answer(f"⚠️ {e}\n\nПопробуй снова (например: `00:00-06:00`):", parse_mode="Markdown")
        return
    window_id = await db.add_window(days_str, start.strftime("%H:%M"), end.strftime("%H:%M"))
    await state.clear()
    days = [int(x) for x in days_str.split(",") if x]
    windows = await db.list_windows()
    await message.answer(
        f"✅ Окно #{window_id} добавлено: *{format_days(days)}* `{start.strftime('%H:%M')}–{end.strftime('%H:%M')}`",
        parse_mode="Markdown",
        reply_markup=kb_windows(windows),
    )



# ─── entry point ─────────────────────────────────────────────────────────────

async def main() -> None:
    global cfg, db, tz, summarizer, fetcher
    cfg = load_config()
    tz = ZoneInfo(cfg.timezone)

    db_dir = os.path.dirname(cfg.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db = Database(cfg.db_path)
    await db.connect()
    summarizer = Summarizer(cfg.openai_api_key, cfg.openai_model)
    fetcher = HistoryFetcher(cfg)
    await fetcher.start()

    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        _digest_tick,
        "interval",
        seconds=60,
        next_run_time=datetime.now(tz) + timedelta(seconds=5),
        args=[bot],
    )
    scheduler.start()

    try:
        await dp.start_polling(bot)
    finally:
        await fetcher.stop()


if __name__ == "__main__":
    asyncio.run(main())

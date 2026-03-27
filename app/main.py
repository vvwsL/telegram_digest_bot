from __future__ import annotations

import asyncio
import hashlib
import shlex
from datetime import datetime, timezone, timedelta, time as dtime
import os
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import load_config, Config
from .db import Database, Topic, Source, MessageRow
from .summarizer import Summarizer, DigestItem
from .windows import parse_days, parse_time_range, is_dt_in_window, window_end_for_now, format_days


cfg: Config | None = None
db: Database | None = None
summarizer: Summarizer | None = None
tz: ZoneInfo | None = None
digest_lock = asyncio.Lock()

router = Router()


def _is_admin(message: Message) -> bool:
    if cfg is None or message.from_user is None:
        return False
    return message.from_user.id in cfg.admin_ids


async def _deny_if_not_admin(message: Message) -> bool:
    if not _is_admin(message):
        await message.answer("Нет доступа.")
        return True
    return False


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().split())


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_snippet(text: str, max_chars: int) -> str:
    text = _normalize_text(text)
    if not text:
        return ""
    for sep in (".", "!", "?"):
        idx = text.find(sep)
        if 0 < idx < max_chars:
            return text[: idx + 1]
    return text[:max_chars]


def _parse_args(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _parse_time_str(raw: str) -> dtime:
    hour, minute = raw.split(":")
    return dtime(hour=int(hour), minute=int(minute))


async def _get_source_from_arg(message: Message, arg: str | None) -> Source | None:
    assert db is not None
    if arg:
        cleaned = arg.lstrip("@")
        if cleaned.lstrip("-").isdigit():
            source = await db.get_source_by_chat_id(int(cleaned))
            if source:
                return source
        source = await db.get_source_by_username(cleaned)
        if source:
            return source
        try:
            chat = await message.bot.get_chat(cleaned)
        except Exception:
            return None
        return await _upsert_source_from_chat(chat)

    if message.reply_to_message:
        fwd_chat = _extract_forwarded_chat(message.reply_to_message)
        if fwd_chat:
            return await _upsert_source_from_chat(fwd_chat)

    return None


def _extract_forwarded_chat(message: Message):
    if message.forward_from_chat:
        return message.forward_from_chat
    origin = getattr(message, "forward_origin", None)
    if origin and hasattr(origin, "chat"):
        return origin.chat
    return None


async def _upsert_source_from_chat(chat) -> Source:
    assert db is not None
    source_id = await db.add_source(chat_id=chat.id, username=chat.username, title=chat.title)
    source = await db.get_source_by_chat_id(chat.id)
    if source is None:
        raise RuntimeError("Failed to create source")
    return source


async def _is_in_active_window(now: datetime) -> bool:
    assert db is not None
    windows = await db.list_windows()
    if not windows:
        return False
    for row in windows:
        days = [int(x) for x in row.days.split(",") if x]
        start = _parse_time_str(row.start)
        end = _parse_time_str(row.end)
        if is_dt_in_window(now, days, start, end):
            return True
    return False


async def _collect_message(message: Message) -> None:
    if db is None or cfg is None or tz is None:
        return

    content = message.text or message.caption
    if not content:
        return

    if message.text and message.text.startswith("/"):
        return

    source = await db.get_source_by_chat_id(message.chat.id)
    if not source:
        return

    now_local = datetime.now(tz)
    if not await _is_in_active_window(now_local):
        return

    text = _normalize_text(content)
    if not text:
        return
    snippet = _extract_snippet(text, cfg.snippet_chars)
    msg_hash = _make_hash(text.lower())
    msg_dt = message.date
    if msg_dt.tzinfo is None:
        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
    ts = int(msg_dt.timestamp())
    link = None
    if source.username:
        link = f"https://t.me/{source.username}/{message.message_id}"

    await db.add_message(
        source_id=source.id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        ts=ts,
        text=text,
        snippet=snippet,
        link=link,
        msg_hash=msg_hash,
    )


async def _build_digest_items(topic: Topic, start_ts: int, end_ts: int) -> list[DigestItem]:
    assert db is not None and cfg is not None
    messages = await db.fetch_messages_for_topic_window(topic.id, start_ts, end_ts)
    keywords = [kw.casefold() for kw in await db.list_keywords(topic.id)]

    important: list[MessageRow] = []
    normal: list[MessageRow] = []
    for msg in messages:
        text = msg.text.casefold()
        if keywords and any(kw in text for kw in keywords):
            important.append(msg)
        else:
            normal.append(msg)

    selected: list[MessageRow] = []
    seen: set[int] = set()
    important_ids = {msg.id for msg in important}
    for msg in important:
        if msg.id in seen:
            continue
        selected.append(msg)
        seen.add(msg.id)

    for msg in normal[: cfg.max_items_per_topic]:
        if msg.id in seen:
            continue
        selected.append(msg)
        seen.add(msg.id)

    items: list[DigestItem] = []
    for msg in selected:
        source = msg.source_username or msg.source_title or str(msg.chat_id)
        if msg.source_username:
            source = f"@{msg.source_username}"
        items.append(
            DigestItem(
                snippet=msg.snippet,
                source=source,
                link=msg.link,
                important=msg.id in important_ids,
            )
        )
    return items


async def _digest_tick(bot: Bot) -> None:
    assert db is not None and tz is not None and cfg is not None and summarizer is not None
    async with digest_lock:
        windows = await db.list_windows()
        if not windows:
            return

        output_chat_id_raw = await db.get_setting("output_chat_id")
        if not output_chat_id_raw:
            return
        output_chat_id = int(output_chat_id_raw)

        now = datetime.now(tz)
        topics = await db.list_topics()
        if not topics:
            return

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
                    items = await _build_digest_items(topic, start_ts, end_ts)
                    if not items:
                        await db.finish_digest(topic.id, row.id, end_ts, "empty", None)
                        continue
                    text = await summarizer.summarize(topic.name, start_dt, end_dt, items)
                    if text:
                        await bot.send_message(output_chat_id, text)
                        await db.finish_digest(topic.id, row.id, end_ts, "sent", None)
                    else:
                        await db.finish_digest(topic.id, row.id, end_ts, "empty", "LLM returned empty")
                except Exception as exc:
                    await db.finish_digest(topic.id, row.id, end_ts, "error", str(exc))


@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    text = (
        "Команды:\n"
        "/topic_add <name>\n"
        "/topic_rename <old> <new>\n"
        "/topic_remove <name>\n"
        "/topic_list\n"
        "/topic_src_add <topic> @channel (или ответом на forward)\n"
        "/topic_src_remove <topic> @channel (или ответом на forward)\n"
        "/topic_kw_add <topic> <keyword>\n"
        "/topic_kw_remove <topic> <keyword>\n"
        "/topic_kw_list <topic>\n"
        "/window_add <days> <HH:MM-HH:MM>\n"
        "/window_remove <id>\n"
        "/window_list\n"
        "/setoutput\n"
        "/status"
    )
    await message.answer(text)


@router.message(Command("topic_add"))
async def cmd_topic_add(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    name = " ".join(args[1:]).strip() if len(args) > 1 else ""
    if not name:
        await message.answer("Использование: /topic_add <name>")
        return
    assert db is not None
    topic_id = await db.add_topic(name)
    if topic_id is None:
        await message.answer("Тема уже существует.")
    else:
        await message.answer(f"Тема добавлена: {name}")


@router.message(Command("topic_rename"))
async def cmd_topic_rename(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    if len(args) < 3:
        await message.answer("Использование: /topic_rename <old> <new>")
        return
    old = args[1]
    new = " ".join(args[2:])
    assert db is not None
    ok = await db.rename_topic(old, new)
    await message.answer("Ок." if ok else "Тема не найдена.")


@router.message(Command("topic_remove"))
async def cmd_topic_remove(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    name = " ".join(args[1:]).strip() if len(args) > 1 else ""
    if not name:
        await message.answer("Использование: /topic_remove <name>")
        return
    assert db is not None
    ok = await db.remove_topic(name)
    await message.answer("Удалено." if ok else "Тема не найдена.")


@router.message(Command("topic_list"))
async def cmd_topic_list(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    assert db is not None
    topics = await db.list_topics()
    if not topics:
        await message.answer("Тем пока нет.")
        return
    lines = ["Темы:"]
    for topic in topics:
        sources = await db.list_topic_sources(topic.id)
        keywords = await db.list_keywords(topic.id)
        lines.append(f"- {topic.name} (источников: {len(sources)}, ключей: {len(keywords)})")
    await message.answer("\n".join(lines))


@router.message(Command("topic_src_add"))
async def cmd_topic_src_add(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    if len(args) < 2 and not (message.reply_to_message and message.reply_to_message.forward_from_chat):
        await message.answer("Использование: /topic_src_add <topic> @channel (или ответом на forward)")
        return
    if len(args) >= 3:
        topic_name = " ".join(args[1:-1]).strip()
        source_arg = args[-1]
    elif len(args) == 2:
        topic_name = args[1]
        source_arg = None
    else:
        topic_name = ""
        source_arg = None
    if not topic_name:
        await message.answer("Не указана тема.")
        return
    assert db is not None
    topic = await db.get_topic(topic_name)
    if not topic:
        await message.answer("Тема не найдена.")
        return
    source = await _get_source_from_arg(message, source_arg)
    if not source:
        await message.answer("Не удалось определить источник. Укажи @username или ответь на forward.")
        return
    linked = await db.link_topic_source(topic.id, source.id)
    await message.answer("Источник привязан." if linked else "Источник уже привязан.")


@router.message(Command("topic_src_remove"))
async def cmd_topic_src_remove(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    if len(args) < 2 and not (message.reply_to_message and message.reply_to_message.forward_from_chat):
        await message.answer("Использование: /topic_src_remove <topic> @channel (или ответом на forward)")
        return
    if len(args) >= 3:
        topic_name = " ".join(args[1:-1]).strip()
        source_arg = args[-1]
    elif len(args) == 2:
        topic_name = args[1]
        source_arg = None
    else:
        topic_name = ""
        source_arg = None
    if not topic_name:
        await message.answer("Не указана тема.")
        return
    assert db is not None
    topic = await db.get_topic(topic_name)
    if not topic:
        await message.answer("Тема не найдена.")
        return
    source = await _get_source_from_arg(message, source_arg)
    if not source:
        await message.answer("Не удалось определить источник.")
        return
    unlinked = await db.unlink_topic_source(topic.id, source.id)
    await message.answer("Источник отвязан." if unlinked else "Связка не найдена.")


@router.message(Command("topic_kw_add"))
async def cmd_topic_kw_add(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    if len(args) < 3:
        await message.answer("Использование: /topic_kw_add <topic> <keyword>")
        return
    topic_name = args[1]
    keyword = " ".join(args[2:]).strip()
    assert db is not None
    topic = await db.get_topic(topic_name)
    if not topic:
        await message.answer("Тема не найдена.")
        return
    ok = await db.add_keyword(topic.id, keyword)
    await message.answer("Ключ добавлен." if ok else "Ключ уже есть.")


@router.message(Command("topic_kw_remove"))
async def cmd_topic_kw_remove(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    if len(args) < 3:
        await message.answer("Использование: /topic_kw_remove <topic> <keyword>")
        return
    topic_name = args[1]
    keyword = " ".join(args[2:]).strip()
    assert db is not None
    topic = await db.get_topic(topic_name)
    if not topic:
        await message.answer("Тема не найдена.")
        return
    ok = await db.remove_keyword(topic.id, keyword)
    await message.answer("Ключ удален." if ok else "Ключ не найден.")


@router.message(Command("topic_kw_list"))
async def cmd_topic_kw_list(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    if len(args) < 2:
        await message.answer("Использование: /topic_kw_list <topic>")
        return
    topic_name = args[1]
    assert db is not None
    topic = await db.get_topic(topic_name)
    if not topic:
        await message.answer("Тема не найдена.")
        return
    keywords = await db.list_keywords(topic.id)
    if not keywords:
        await message.answer("Ключей нет.")
        return
    await message.answer("Ключи:\n" + "\n".join(f"- {kw}" for kw in keywords))


@router.message(Command("window_add"))
async def cmd_window_add(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    if len(args) < 3:
        await message.answer("Использование: /window_add <days> <HH:MM-HH:MM>")
        return
    days_raw = args[1]
    time_raw = args[2]
    try:
        days = parse_days(days_raw)
        start, end = parse_time_range(time_raw)
    except ValueError as exc:
        await message.answer(f"Ошибка: {exc}")
        return
    assert db is not None
    days_str = ",".join(str(d) for d in days)
    window_id = await db.add_window(days_str, start.strftime("%H:%M"), end.strftime("%H:%M"))
    await message.answer(
        f"Ок. Окно #{window_id}: {format_days(days)} {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    )


@router.message(Command("window_remove"))
async def cmd_window_remove(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    args = _parse_args(message.text or "")
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: /window_remove <id>")
        return
    window_id = int(args[1])
    assert db is not None
    ok = await db.remove_window(window_id)
    await message.answer("Удалено." if ok else "Окно не найдено.")


@router.message(Command("window_list"))
async def cmd_window_list(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    assert db is not None
    windows = await db.list_windows()
    if not windows:
        await message.answer("Окон пока нет.")
        return
    lines = ["Окна:"]
    for row in windows:
        days = [int(x) for x in row.days.split(",") if x]
        lines.append(f"- #{row.id}: {format_days(days)} {row.start}-{row.end}")
    await message.answer("\n".join(lines))


@router.message(Command("setoutput"))
async def cmd_setoutput(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    assert db is not None
    await db.set_setting("output_chat_id", str(message.chat.id))
    await message.answer("Чат для дайджестов установлен.")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if await _deny_if_not_admin(message):
        return
    assert db is not None
    topics = await db.list_topics()
    windows = await db.list_windows()
    output_chat_id = await db.get_setting("output_chat_id")
    lines = [
        f"Тем: {len(topics)}",
        f"Окон: {len(windows)}",
        f"Чат для дайджестов: {output_chat_id or 'не задан'}",
    ]
    await message.answer("\n".join(lines))


@router.message()
async def handle_any_message(message: Message) -> None:
    await _collect_message(message)


async def main() -> None:
    global cfg, db, tz, summarizer
    cfg = load_config()
    tz = ZoneInfo(cfg.timezone)

    db_dir = os.path.dirname(cfg.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db = Database(cfg.db_path)
    await db.connect()
    summarizer = Summarizer(cfg.openai_api_key, cfg.openai_model)

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

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

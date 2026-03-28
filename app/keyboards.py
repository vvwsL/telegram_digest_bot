from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .db import Topic, Source, WindowRow
from .windows import format_days, DAY_NAMES_RU


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb_main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        _btn("📋 Темы", "menu:topics"),
        _btn("⏰ Окна", "menu:windows"),
    )
    b.row(
        _btn("📤 Задать output-чат", "action:setoutput"),
        _btn("📊 Статус", "action:status"),
    )
    b.row(_btn("🚨 Экстренная сводка (30 постов)", "action:emergency_digest"))
    return b.as_markup()


def kb_topics(topics: list[Topic]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in topics:
        b.row(_btn(f"📌 {t.name}", f"topic:show:{t.id}"))
    b.row(_btn("➕ Новая тема", "action:add_topic"))
    b.row(_btn("🔙 Главное меню", "menu:main"))
    return b.as_markup()


def kb_topic_detail(topic: Topic) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        _btn("📡 Источники", f"topic:sources:{topic.id}"),
        _btn("🔑 Ключевые слова", f"topic:keywords:{topic.id}"),
    )
    b.row(
        _btn("✏️ Переименовать", f"topic:rename:{topic.id}"),
        _btn("🗑️ Удалить тему", f"topic:del_confirm:{topic.id}"),
    )
    b.row(_btn("🔙 К темам", "menu:topics"))
    return b.as_markup()


def kb_topic_del_confirm(topic: Topic) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        _btn("✅ Да, удалить", f"topic:del:{topic.id}"),
        _btn("❌ Отмена", f"topic:show:{topic.id}"),
    )
    return b.as_markup()


def kb_sources(topic: Topic, sources: list[Source]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in sources:
        label = f"@{s.username}" if s.username else (s.title or str(s.chat_id))
        b.row(
            InlineKeyboardButton(text=f"📡 {label}", callback_data=f"noop"),
            _btn("🗑️", f"src:del:{topic.id}:{s.id}"),
        )
    b.row(_btn("➕ Добавить источник", f"topic:src_add:{topic.id}"))
    b.row(_btn("🔙 К теме", f"topic:show:{topic.id}"))
    return b.as_markup()


def kb_keywords(topic: Topic, keywords: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for kw in keywords:
        # Store index to avoid 64-byte callback limit on long keywords
        safe = kw[:30]
        b.row(
            InlineKeyboardButton(text=f"🔑 {kw}", callback_data="noop"),
            _btn("🗑️", f"kw:del:{topic.id}:{safe}"),
        )
    b.row(_btn("➕ Добавить ключевое слово", f"topic:kw_add:{topic.id}"))
    b.row(_btn("🔙 К теме", f"topic:show:{topic.id}"))
    return b.as_markup()


def kb_windows(windows: list[WindowRow]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for w in windows:
        days = [int(x) for x in w.days.split(",") if x]
        label = f"#{w.id} {format_days(days)} {w.start}–{w.end}"
        b.row(
            InlineKeyboardButton(text=f"⏰ {label}", callback_data="noop"),
            _btn("🗑️", f"window:del:{w.id}"),
        )
    b.row(_btn("➕ Новое окно", "action:add_window"))
    b.row(_btn("🔙 Главное меню", "menu:main"))
    return b.as_markup()


def kb_cancel(back_cb: str = "menu:main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("❌ Отмена", back_cb))
    return b.as_markup()


def kb_back(cb: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("🔙 Назад", cb))
    return b.as_markup()


def kb_status_detail() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("🔢 Статистика токенов", "action:token_stats"))
    b.row(_btn("🔙 Главное меню", "menu:main"))
    return b.as_markup()

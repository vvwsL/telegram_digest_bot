from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .db import Channel


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("📢 Каналы", "ch:list"), _btn("🔑 Ключевые слова", "kw:list"))
    b.row(_btn("⏰ Расписание", "sch:list"), _btn("📤 Куда слать", "out:show"))
    b.row(_btn("🚀 Дайджест сейчас", "digest:now"))
    b.row(_btn("📊 Статус", "status:show"))
    return b.as_markup()


def kb_channels(channels: list[Channel]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for ch in channels:
        label = f"@{ch.username}" if ch.username else (ch.title or str(ch.chat_id))
        b.row(
            InlineKeyboardButton(text=f"📢 {label[:35]}", callback_data="noop"),
            _btn("🗑️", f"ch:del:{ch.id}"),
        )
    b.row(_btn("➕ Добавить (Bot API)", "ch:add"), _btn("🌐 Добавить (Web)", "ch:add_web"))
    b.row(_btn("🔙 Главное меню", "main:menu"))
    return b.as_markup()


def kb_keywords(keywords: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for kw_id, kw in keywords:
        b.row(
            InlineKeyboardButton(text=f"🔑 {kw[:35]}", callback_data="noop"),
            _btn("🗑️", f"kw:del:{kw_id}"),
        )
    b.row(_btn("➕ Добавить слово", "kw:add"))
    b.row(_btn("🔙 Главное меню", "main:menu"))
    return b.as_markup()


def kb_schedule(times: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for sch_id, t in times:
        b.row(
            InlineKeyboardButton(text=f"⏰ {t}", callback_data="noop"),
            _btn("🗑️", f"sch:del:{sch_id}"),
        )
    b.row(_btn("➕ Добавить время", "sch:add"))
    b.row(_btn("🔙 Главное меню", "main:menu"))
    return b.as_markup()


def kb_output(current: str | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("✏️ Изменить", "out:set"))
    b.row(_btn("🔙 Главное меню", "main:menu"))
    return b.as_markup()


def kb_cancel() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("❌ Отмена", "main:menu"))
    return b.as_markup()


def kb_back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("🔙 Главное меню", "main:menu"))
    return b.as_markup()

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .db import Channel, Folder


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("📁 Папки", "fold:list"), _btn("🔑 Ключевые слова", "kw:list"))
    b.row(_btn("⏰ Расписание", "sch:list"), _btn("📤 Куда слать", "out:show"))
    b.row(_btn("🚀 Дайджест сейчас", "digest:now"), _btn("📅 Разовый", "digest:oneoff"))
    b.row(_btn("📊 Статус", "status:show"))
    return b.as_markup()


def kb_folders(folders: list[Folder], counts: dict[int, int]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for f in folders:
        cnt = counts.get(f.id, 0)
        b.row(
            _btn(f"{f.name[:30]} ({cnt})", f"fold:view:{f.id}"),
            _btn("🗑️", f"fold:del:{f.id}"),
        )
    b.row(_btn("➕ Создать папку", "fold:add"))
    b.row(_btn("🔙 Главное меню", "main:menu"))
    return b.as_markup()


def kb_folder(folder: Folder, channels: list[Channel]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for ch in channels:
        label = f"@{ch.username}" if ch.username else (ch.title or str(ch.chat_id))
        b.row(
            _btn(f"📢 {label[:32]}", "noop"),
            _btn("🗑️", f"ch:del:{ch.id}"),
        )
    b.row(_btn("➕ Добавить канал", f"ch:add_in:{folder.id}"))
    b.row(_btn("📝 Промт LLM", f"fold:prompt:{folder.id}"))
    b.row(_btn("🚀 Дайджест папки сейчас", f"fold:digest:{folder.id}"))
    b.row(_btn("🔙 К папкам", "fold:list"))
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


def kb_digest_period() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("📅 1 день", "oneoff:1d"), _btn("📅 3 дня", "oneoff:3d"))
    b.row(_btn("📅 1 неделя", "oneoff:7d"), _btn("📅 2 недели", "oneoff:14d"))
    b.row(_btn("📅 1 месяц", "oneoff:30d"), _btn("📅 2 месяца", "oneoff:60d"))
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


def kb_back_to_folders() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("🔙 К папкам", "fold:list"))
    return b.as_markup()


def kb_back_to_folder(folder_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("🔙 Назад", f"fold:view:{folder_id}"))
    return b.as_markup()

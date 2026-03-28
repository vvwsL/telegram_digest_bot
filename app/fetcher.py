from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import timezone

from pyrogram import Client
from pyrogram.errors import ChannelInvalid, ChannelPrivate, UsernameInvalid, UsernameNotOccupied, PeerIdInvalid
from pyrogram.types import Message as PyroMessage

from .db import Database
from .config import Config


def _normalize(text: str) -> str:
    return " ".join(text.strip().split())


def _snippet(text: str, max_chars: int) -> str:
    text = _normalize(text)
    if not text:
        return ""
    for sep in (".", "!", "?"):
        idx = text.find(sep)
        if 0 < idx < max_chars:
            return text[: idx + 1]
    return text[:max_chars]


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class ChannelInfo:
    chat_id: int
    username: str | None
    title: str | None


class HistoryFetcher:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        session_path = os.path.join(os.path.dirname(cfg.db_path), "pyrogram_session")
        self._client = Client(
            name=session_path,
            api_id=cfg.tg_api_id,
            api_hash=cfg.tg_api_hash,
            bot_token=cfg.bot_token,
        )

    async def start(self) -> None:
        await self._client.start()

    async def stop(self) -> None:
        await self._client.stop()

    async def resolve_channel(self, identifier: str) -> ChannelInfo | None:
        try:
            cleaned = identifier.lstrip("@")
            chat = await self._client.get_chat(int(cleaned) if cleaned.lstrip("-").isdigit() else cleaned)
            return ChannelInfo(chat_id=chat.id, username=chat.username, title=chat.title)
        except (ChannelInvalid, ChannelPrivate, UsernameInvalid, UsernameNotOccupied, PeerIdInvalid):
            return None
        except Exception:
            return None

    async def fetch_and_store(
        self,
        db: Database,
        channel_id: int,
        chat_id: int,
        username: str | None,
        limit: int,
        since_ts: int | None = None,
    ) -> int:
        stored = 0
        chat_ref: str | int = f"@{username}" if username else chat_id
        try:
            async for msg in self._client.get_chat_history(chat_ref, limit=limit):
                msg: PyroMessage
                content = msg.text or msg.caption
                if not content:
                    continue

                msg_dt = msg.date
                if msg_dt.tzinfo is None:
                    msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                ts = int(msg_dt.timestamp())

                if since_ts is not None and ts <= since_ts:
                    break

                text = _normalize(content)
                if not text:
                    continue

                snippet = _snippet(text, self._cfg.snippet_chars)
                msg_hash = _make_hash(text.lower())
                link = f"https://t.me/{username}/{msg.id}" if username else None

                added = await db.add_message(
                    channel_id=channel_id,
                    chat_id=chat_id,
                    message_id=msg.id,
                    ts=ts,
                    text=text,
                    snippet=snippet,
                    link=link,
                    msg_hash=msg_hash,
                )
                if added:
                    stored += 1
        except (ChannelPrivate, ChannelInvalid):
            pass

        return stored

    async def fetch_all_channels(
        self,
        db: Database,
        since_ts: int | None,
        limit: int,
    ) -> int:
        channels = await db.list_channels()
        total = 0
        for ch in channels:
            total += await self.fetch_and_store(
                db,
                channel_id=ch.id,
                chat_id=ch.chat_id,
                username=ch.username,
                limit=limit,
                since_ts=since_ts,
            )
        return total

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
    """
    Uses Pyrogram (MTProto) to actively pull posts from channels.

    Public channels (@username): works without the bot being a member.
    Private channels: bot must be added as admin.
    """

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
        """
        Resolve a channel by @username or numeric ID via MTProto.
        Works for any public channel — the bot does NOT need to be a member.
        Returns None if the channel cannot be found or is inaccessible.
        """
        try:
            cleaned = identifier.lstrip("@")
            # Try as numeric ID first
            if cleaned.lstrip("-").isdigit():
                chat = await self._client.get_chat(int(cleaned))
            else:
                chat = await self._client.get_chat(cleaned)
            return ChannelInfo(
                chat_id=chat.id,
                username=chat.username,
                title=chat.title,
            )
        except (ChannelInvalid, ChannelPrivate, UsernameInvalid, UsernameNotOccupied, PeerIdInvalid):
            return None
        except Exception:
            return None

    def _chat_ref(self, chat_id: int, username: str | None) -> str | int:
        """Use @username for public channels (works without membership), int id otherwise."""
        return f"@{username}" if username else chat_id

    async def fetch_and_store(
        self,
        db: Database,
        source_id: int,
        chat_id: int,
        username: str | None,
        limit: int,
        since_ts: int | None = None,
    ) -> int:
        """
        Pull up to `limit` messages from the channel via MTProto.
        If `since_ts` is given, stops when it hits messages older than that.
        Returns number of new messages stored.
        """
        stored = 0
        chat_ref = self._chat_ref(chat_id, username)
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
                    source_id=source_id,
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
            # Private channel bot has no access to — skip silently
            pass

        return stored

    async def fetch_all_sources_for_digest(
        self,
        db: Database,
        topic_id: int,
        since_ts: int | None,
        until_ts: int,
        first_run: bool,
        history_limit: int,
    ) -> int:
        """Fetch messages for all sources linked to a topic."""
        sources = await db.list_topic_sources(topic_id)
        total = 0
        for source in sources:
            if first_run:
                n = await self.fetch_and_store(
                    db, source.id, source.chat_id, source.username,
                    limit=history_limit,
                    since_ts=None,
                )
            else:
                period_seconds = until_ts - (since_ts or 0)
                approx_limit = max(50, period_seconds // 600)
                n = await self.fetch_and_store(
                    db, source.id, source.chat_id, source.username,
                    limit=approx_limit,
                    since_ts=since_ts,
                )
            total += n
        return total

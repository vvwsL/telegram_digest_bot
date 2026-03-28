from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
import aiosqlite


@dataclass(frozen=True)
class Channel:
    id: int
    chat_id: int
    username: str | None
    title: str | None


@dataclass(frozen=True)
class MessageRow:
    id: int
    channel_id: int
    chat_id: int
    message_id: int
    ts: int
    text: str
    snippet: str
    link: str | None
    hash: str
    channel_username: str | None
    channel_title: str | None


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        async with self._conn.execute("PRAGMA journal_mode=WAL"):
            pass
        async with self._conn.execute("PRAGMA foreign_keys=ON"):
            pass
        await self._init_schema()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def _init_schema(self) -> None:
        assert self._conn is not None
        schema = """
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            text TEXT NOT NULL,
            snippet TEXT NOT NULL,
            link TEXT,
            hash TEXT NOT NULL,
            UNIQUE(channel_id, hash),
            FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
        async with self._lock:
            await self._conn.executescript(schema)
            await self._conn.commit()

    async def _execute(self, query: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(query, params)
            await self._conn.commit()
            return cur

    async def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(query, params)
            return await cur.fetchone()

    async def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(query, params)
            return await cur.fetchall()

    # ── Channels ──────────────────────────────────────────────────────────────

    async def add_channel(self, chat_id: int, username: str | None, title: str | None) -> int:
        cur = await self._execute(
            "INSERT OR IGNORE INTO channels(chat_id, username, title) VALUES (?, ?, ?)",
            (chat_id, username, title),
        )
        if cur.rowcount == 0:
            row = await self._fetchone("SELECT id FROM channels WHERE chat_id = ?", (chat_id,))
            return row["id"]
        return cur.lastrowid

    async def remove_channel(self, channel_id: int) -> bool:
        cur = await self._execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        return cur.rowcount > 0

    async def list_channels(self) -> list[Channel]:
        rows = await self._fetchall(
            "SELECT id, chat_id, username, title FROM channels ORDER BY COALESCE(title, username, chat_id)"
        )
        return [Channel(id=r["id"], chat_id=r["chat_id"], username=r["username"], title=r["title"]) for r in rows]

    async def get_channel_by_chat_id(self, chat_id: int) -> Channel | None:
        row = await self._fetchone(
            "SELECT id, chat_id, username, title FROM channels WHERE chat_id = ?", (chat_id,)
        )
        if not row:
            return None
        return Channel(id=row["id"], chat_id=row["chat_id"], username=row["username"], title=row["title"])

    # ── Schedule ──────────────────────────────────────────────────────────────

    async def add_schedule_time(self, time: str) -> bool:
        cur = await self._execute("INSERT OR IGNORE INTO schedule(time) VALUES (?)", (time,))
        return cur.rowcount > 0

    async def remove_schedule_time(self, schedule_id: int) -> bool:
        cur = await self._execute("DELETE FROM schedule WHERE id = ?", (schedule_id,))
        return cur.rowcount > 0

    async def list_schedule(self) -> list[tuple[int, str]]:
        rows = await self._fetchall("SELECT id, time FROM schedule ORDER BY time")
        return [(r["id"], r["time"]) for r in rows]

    # ── Keywords ─────────────────────────────────────────────────────────────

    async def add_keyword(self, keyword: str) -> bool:
        cur = await self._execute(
            "INSERT OR IGNORE INTO keywords(keyword) VALUES (?)", (keyword.lower().strip(),)
        )
        return cur.rowcount > 0

    async def remove_keyword(self, kw_id: int) -> bool:
        cur = await self._execute("DELETE FROM keywords WHERE id = ?", (kw_id,))
        return cur.rowcount > 0

    async def list_keywords(self) -> list[tuple[int, str]]:
        rows = await self._fetchall("SELECT id, keyword FROM keywords ORDER BY keyword")
        return [(r["id"], r["keyword"]) for r in rows]

    # ── Messages ─────────────────────────────────────────────────────────────

    async def add_message(
        self,
        channel_id: int,
        chat_id: int,
        message_id: int,
        ts: int,
        text: str,
        snippet: str,
        link: str | None,
        msg_hash: str,
    ) -> bool:
        cur = await self._execute(
            """
            INSERT OR IGNORE INTO messages(channel_id, chat_id, message_id, ts, text, snippet, link, hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_id, chat_id, message_id, ts, text, snippet, link, msg_hash),
        )
        return cur.rowcount > 0

    async def fetch_messages_since(self, since_ts: int, until_ts: int) -> list[MessageRow]:
        rows = await self._fetchall(
            """
            SELECT m.id, m.channel_id, m.chat_id, m.message_id, m.ts,
                   m.text, m.snippet, m.link, m.hash,
                   c.username AS channel_username, c.title AS channel_title
            FROM messages m
            JOIN channels c ON c.id = m.channel_id
            WHERE m.ts >= ? AND m.ts < ?
            ORDER BY m.ts DESC
            """,
            (since_ts, until_ts),
        )
        return [_msg_row(r) for r in rows]

    async def fetch_recent_messages(self, limit: int = 30) -> list[MessageRow]:
        rows = await self._fetchall(
            """
            SELECT m.id, m.channel_id, m.chat_id, m.message_id, m.ts,
                   m.text, m.snippet, m.link, m.hash,
                   c.username AS channel_username, c.title AS channel_title
            FROM messages m
            JOIN channels c ON c.id = m.channel_id
            ORDER BY m.ts DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [_msg_row(r) for r in rows]

    # ── Digests ──────────────────────────────────────────────────────────────

    async def create_digest(self, ts: int, status: str, tokens_in: int = 0, tokens_out: int = 0) -> int:
        cur = await self._execute(
            "INSERT INTO digests(ts, status, tokens_in, tokens_out) VALUES (?, ?, ?, ?)",
            (ts, status, tokens_in, tokens_out),
        )
        return cur.lastrowid

    async def get_token_stats(self) -> dict:
        row = await self._fetchone(
            "SELECT SUM(tokens_in) as ti, SUM(tokens_out) as to_, COUNT(*) as cnt "
            "FROM digests WHERE status = 'sent'"
        )
        return {
            "total_in": row["ti"] or 0,
            "total_out": row["to_"] or 0,
            "count": row["cnt"] or 0,
        }

    # ── Settings ─────────────────────────────────────────────────────────────

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def get_setting(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None


def _msg_row(r: aiosqlite.Row) -> MessageRow:
    return MessageRow(
        id=r["id"],
        channel_id=r["channel_id"],
        chat_id=r["chat_id"],
        message_id=r["message_id"],
        ts=r["ts"],
        text=r["text"],
        snippet=r["snippet"],
        link=r["link"],
        hash=r["hash"],
        channel_username=r["channel_username"],
        channel_title=r["channel_title"],
    )

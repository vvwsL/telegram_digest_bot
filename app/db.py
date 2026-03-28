from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
import aiosqlite


@dataclass(frozen=True)
class Topic:
    id: int
    name: str


@dataclass(frozen=True)
class Source:
    id: int
    chat_id: int
    username: str | None
    title: str | None


@dataclass(frozen=True)
class WindowRow:
    id: int
    days: str
    start: str
    end: str


@dataclass(frozen=True)
class MessageRow:
    id: int
    source_id: int
    chat_id: int
    message_id: int
    ts: int
    text: str
    snippet: str
    link: str | None
    hash: str
    source_username: str | None
    source_title: str | None


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
        await self._migrate()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def _init_schema(self) -> None:
        assert self._conn is not None
        schema = """
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS topic_sources (
            topic_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            UNIQUE(topic_id, source_id),
            FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE,
            FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            days TEXT NOT NULL,
            start TEXT NOT NULL,
            end TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS topic_keywords (
            topic_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            UNIQUE(topic_id, keyword),
            FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            text TEXT NOT NULL,
            snippet TEXT NOT NULL,
            link TEXT,
            hash TEXT NOT NULL,
            UNIQUE(source_id, hash),
            FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            window_id INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            UNIQUE(topic_id, window_id, end_ts),
            FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
        );
        -- window_id=0 is reserved for emergency digests (no FK constraint)
        -- migration: add token columns if not present
        CREATE TABLE IF NOT EXISTS _migrations(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
        async with self._lock:
            await self._conn.executescript(schema)
            await self._conn.commit()

    async def _migrate(self) -> None:
        """Apply one-time schema migrations for existing databases."""
        assert self._conn is not None
        async with self._lock:
            for col in ("tokens_in", "tokens_out"):
                try:
                    await self._conn.execute(
                        f"ALTER TABLE digests ADD COLUMN {col} INTEGER DEFAULT 0"
                    )
                    await self._conn.commit()
                except Exception:
                    pass  # column already exists

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
            row = await cur.fetchone()
            return row

    async def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(query, params)
            rows = await cur.fetchall()
            return rows

    async def add_topic(self, name: str) -> int | None:
        cur = await self._execute("INSERT OR IGNORE INTO topics(name) VALUES (?)", (name,))
        if cur.rowcount == 0:
            return None
        return cur.lastrowid

    async def rename_topic(self, old: str, new: str) -> bool:
        cur = await self._execute("UPDATE topics SET name = ? WHERE name = ?", (new, old))
        return cur.rowcount > 0

    async def rename_topic_by_id(self, topic_id: int, new_name: str) -> bool:
        cur = await self._execute("UPDATE topics SET name = ? WHERE id = ?", (new_name, topic_id))
        return cur.rowcount > 0

    async def remove_topic(self, name: str) -> bool:
        cur = await self._execute("DELETE FROM topics WHERE name = ?", (name,))
        return cur.rowcount > 0

    async def remove_topic_by_id(self, topic_id: int) -> bool:
        cur = await self._execute("DELETE FROM topics WHERE id = ?", (topic_id,))
        return cur.rowcount > 0

    async def list_topics(self) -> list[Topic]:
        rows = await self._fetchall("SELECT id, name FROM topics ORDER BY name")
        return [Topic(id=row["id"], name=row["name"]) for row in rows]

    async def get_topic(self, name: str) -> Topic | None:
        row = await self._fetchone("SELECT id, name FROM topics WHERE name = ?", (name,))
        if row:
            return Topic(id=row["id"], name=row["name"])
        return None

    async def get_topic_by_id(self, topic_id: int) -> Topic | None:
        row = await self._fetchone("SELECT id, name FROM topics WHERE id = ?", (topic_id,))
        if row:
            return Topic(id=row["id"], name=row["name"])
        return None

    async def add_source(self, chat_id: int, username: str | None, title: str | None) -> int | None:
        cur = await self._execute(
            "INSERT OR IGNORE INTO sources(chat_id, username, title) VALUES (?, ?, ?)",
            (chat_id, username, title),
        )
        if cur.rowcount == 0:
            row = await self._fetchone("SELECT id FROM sources WHERE chat_id = ?", (chat_id,))
            return row["id"] if row else None
        return cur.lastrowid

    async def get_source_by_chat_id(self, chat_id: int) -> Source | None:
        row = await self._fetchone(
            "SELECT id, chat_id, username, title FROM sources WHERE chat_id = ?", (chat_id,)
        )
        if not row:
            return None
        return Source(
            id=row["id"],
            chat_id=row["chat_id"],
            username=row["username"],
            title=row["title"],
        )

    async def get_source_by_username(self, username: str) -> Source | None:
        row = await self._fetchone(
            "SELECT id, chat_id, username, title FROM sources WHERE username = ?", (username,)
        )
        if not row:
            return None
        return Source(
            id=row["id"],
            chat_id=row["chat_id"],
            username=row["username"],
            title=row["title"],
        )

    async def link_topic_source(self, topic_id: int, source_id: int) -> bool:
        cur = await self._execute(
            "INSERT OR IGNORE INTO topic_sources(topic_id, source_id) VALUES (?, ?)",
            (topic_id, source_id),
        )
        return cur.rowcount > 0

    async def unlink_topic_source(self, topic_id: int, source_id: int) -> bool:
        cur = await self._execute(
            "DELETE FROM topic_sources WHERE topic_id = ? AND source_id = ?",
            (topic_id, source_id),
        )
        return cur.rowcount > 0

    async def list_topic_sources(self, topic_id: int) -> list[Source]:
        rows = await self._fetchall(
            """
            SELECT s.id, s.chat_id, s.username, s.title
            FROM sources s
            JOIN topic_sources ts ON ts.source_id = s.id
            WHERE ts.topic_id = ?
            ORDER BY COALESCE(s.username, s.title, s.chat_id)
            """,
            (topic_id,),
        )
        return [
            Source(id=row["id"], chat_id=row["chat_id"], username=row["username"], title=row["title"])
            for row in rows
        ]

    async def add_keyword(self, topic_id: int, keyword: str) -> bool:
        cur = await self._execute(
            "INSERT OR IGNORE INTO topic_keywords(topic_id, keyword) VALUES (?, ?)",
            (topic_id, keyword),
        )
        return cur.rowcount > 0

    async def remove_keyword(self, topic_id: int, keyword: str) -> bool:
        cur = await self._execute(
            "DELETE FROM topic_keywords WHERE topic_id = ? AND keyword = ?",
            (topic_id, keyword),
        )
        return cur.rowcount > 0

    async def list_keywords(self, topic_id: int) -> list[str]:
        rows = await self._fetchall(
            "SELECT keyword FROM topic_keywords WHERE topic_id = ? ORDER BY keyword", (topic_id,)
        )
        return [row["keyword"] for row in rows]

    async def add_window(self, days: str, start: str, end: str) -> int | None:
        cur = await self._execute(
            "INSERT INTO windows(days, start, end) VALUES (?, ?, ?)", (days, start, end)
        )
        return cur.lastrowid

    async def remove_window(self, window_id: int) -> bool:
        cur = await self._execute("DELETE FROM windows WHERE id = ?", (window_id,))
        return cur.rowcount > 0

    async def list_windows(self) -> list[WindowRow]:
        rows = await self._fetchall("SELECT id, days, start, end FROM windows ORDER BY id")
        return [WindowRow(id=row["id"], days=row["days"], start=row["start"], end=row["end"]) for row in rows]

    async def add_message(
        self,
        source_id: int,
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
            INSERT OR IGNORE INTO messages(source_id, chat_id, message_id, ts, text, snippet, link, hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_id, chat_id, message_id, ts, text, snippet, link, msg_hash),
        )
        return cur.rowcount > 0

    async def fetch_messages_for_topic_window(
        self, topic_id: int, start_ts: int, end_ts: int
    ) -> list[MessageRow]:
        rows = await self._fetchall(
            """
            SELECT m.id, m.source_id, m.chat_id, m.message_id, m.ts, m.text, m.snippet, m.link, m.hash,
                   s.username AS source_username, s.title AS source_title
            FROM messages m
            JOIN sources s ON s.id = m.source_id
            JOIN topic_sources ts ON ts.source_id = s.id
            WHERE ts.topic_id = ? AND m.ts >= ? AND m.ts < ?
            ORDER BY m.ts DESC
            """,
            (topic_id, start_ts, end_ts),
        )
        return [
            MessageRow(
                id=row["id"],
                source_id=row["source_id"],
                chat_id=row["chat_id"],
                message_id=row["message_id"],
                ts=row["ts"],
                text=row["text"],
                snippet=row["snippet"],
                link=row["link"],
                hash=row["hash"],
                source_username=row["source_username"],
                source_title=row["source_title"],
            )
            for row in rows
        ]

    async def fetch_recent_messages_for_topic(self, topic_id: int, limit: int = 30) -> list[MessageRow]:
        rows = await self._fetchall(
            """
            SELECT m.id, m.source_id, m.chat_id, m.message_id, m.ts, m.text, m.snippet, m.link, m.hash,
                   s.username AS source_username, s.title AS source_title
            FROM messages m
            JOIN sources s ON s.id = m.source_id
            JOIN topic_sources ts ON ts.source_id = s.id
            WHERE ts.topic_id = ?
            ORDER BY m.ts DESC
            LIMIT ?
            """,
            (topic_id, limit),
        )
        return [
            MessageRow(
                id=row["id"], source_id=row["source_id"], chat_id=row["chat_id"],
                message_id=row["message_id"], ts=row["ts"], text=row["text"],
                snippet=row["snippet"], link=row["link"], hash=row["hash"],
                source_username=row["source_username"], source_title=row["source_title"],
            )
            for row in rows
        ]

    async def is_first_digest(self, topic_id: int) -> bool:
        """True if this topic has never had a real digest sent (seeded-only doesn't count)."""
        row = await self._fetchone(
            "SELECT id FROM digests WHERE topic_id = ? AND status IN ('sent', 'empty') LIMIT 1",
            (topic_id,),
        )
        return row is None

    async def create_digest(self, topic_id: int, window_id: int, end_ts: int) -> bool:
        cur = await self._execute(
            "INSERT OR IGNORE INTO digests(topic_id, window_id, end_ts, status) VALUES (?, ?, ?, ?)",
            (topic_id, window_id, end_ts, "running"),
        )
        return cur.rowcount > 0

    async def finish_digest(
        self,
        topic_id: int,
        window_id: int,
        end_ts: int,
        status: str,
        error: str | None,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        await self._execute(
            """UPDATE digests SET status = ?, error = ?, tokens_in = ?, tokens_out = ?
               WHERE topic_id = ? AND window_id = ? AND end_ts = ?""",
            (status, error, tokens_in, tokens_out, topic_id, window_id, end_ts),
        )

    async def log_emergency_tokens(self, topic_id: int, tokens_in: int, tokens_out: int) -> None:
        """Store token usage for an emergency digest (window_id=0, end_ts=now)."""
        import time as _time
        end_ts = int(_time.time())
        await self._execute(
            """INSERT OR IGNORE INTO digests(topic_id, window_id, end_ts, status, tokens_in, tokens_out)
               VALUES (?, 0, ?, 'sent', ?, ?)""",
            (topic_id, end_ts, tokens_in, tokens_out),
        )

    async def get_token_stats(self) -> dict:
        """Returns total and per-topic token usage."""
        row = await self._fetchone(
            "SELECT SUM(tokens_in) as ti, SUM(tokens_out) as to_ FROM digests WHERE status = 'sent'"
        )
        total_in = row["ti"] or 0
        total_out = row["to_"] or 0

        rows = await self._fetchall(
            """SELECT t.name, SUM(d.tokens_in) as ti, SUM(d.tokens_out) as to_, COUNT(*) as cnt
               FROM digests d JOIN topics t ON t.id = d.topic_id
               WHERE d.status = 'sent'
               GROUP BY d.topic_id ORDER BY ti DESC"""
        )
        per_topic = [
            {"name": r["name"], "tokens_in": r["ti"] or 0, "tokens_out": r["to_"] or 0, "count": r["cnt"]}
            for r in rows
        ]
        return {"total_in": total_in, "total_out": total_out, "per_topic": per_topic}

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def get_setting(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

"""
Telegram web-preview scraper — uses t.me/s/<username>.
No API keys, no MTProto, no extra dependencies (stdlib only).
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional


@dataclass
class ScrapedPost:
    message_id: int
    ts: int
    text: str
    link: str


def synthetic_chat_id(username: str) -> int:
    """
    Stable negative int for web-scraped channels.
    Real Telegram channel IDs are like -100xxxxxxxxxx (12+ digits).
    We use the range -1 … -999_999 which Telegram never assigns.
    """
    h = int(hashlib.md5(username.lower().encode()).hexdigest()[:8], 16)
    return -(h % 999_999 + 1)


# ── HTML parser ───────────────────────────────────────────────────────────────

class _TGParser(HTMLParser):
    """
    Parses server-side rendered HTML from t.me/s/<username>.

    Relevant structure (simplified):

        <div class="tgme_widget_message ... " data-post="chan/123">
          ...
          <div class="tgme_widget_message_text ...">
            text / <br> / inline tags
          </div>
          ...
          <a class="tgme_widget_message_date" href="https://t.me/chan/123">
            <time datetime="2024-01-15T10:30:00+00:00">...</time>
          </a>
        </div>
    """

    def __init__(self, username: str) -> None:
        super().__init__(convert_charrefs=True)
        self._username = username.lower().lstrip("@")
        self._posts: list[ScrapedPost] = []

        # accumulator for the message currently being parsed
        self._pid: Optional[int] = None        # message_id
        self._pts: Optional[int] = None        # unix timestamp
        self._plink: Optional[str] = None      # canonical post URL
        self._ptext: list[str] = []            # text fragments

        # state for extracting text from the text-div
        self._in_text: bool = False
        self._text_depth: int = 0              # nested <div> depth inside text-div

    # ── finalization ──────────────────────────────────────────────────────────

    def _flush(self) -> None:
        if self._pid is None or self._pts is None:
            return
        raw = "".join(self._ptext)
        text = re.sub(r"[ \t]+", " ", raw).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if text:
            link = self._plink or f"https://t.me/{self._username}/{self._pid}"
            self._posts.append(ScrapedPost(
                message_id=self._pid,
                ts=self._pts,
                text=text,
                link=link,
            ))
        self._pid = None
        self._pts = None
        self._plink = None
        self._ptext = []

    # ── HTMLParser callbacks ──────────────────────────────────────────────────

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        cls: str = a.get("class", "") or ""

        # ── new message block ────────────────────────────────────────────────
        if tag == "div" and "tgme_widget_message" in cls and "data-post" in a:
            self._flush()
            data_post: str = a.get("data-post") or ""
            parts = data_post.split("/")
            if len(parts) == 2 and parts[1].isdigit():
                self._pid = int(parts[1])
            return

        # ── text div start ───────────────────────────────────────────────────
        if tag == "div" and "tgme_widget_message_text" in cls and self._pid is not None:
            self._in_text = True
            self._text_depth = 0
            self._ptext = []
            return

        # ── inside text div ──────────────────────────────────────────────────
        if self._in_text:
            if tag == "div":
                self._text_depth += 1
            elif tag == "br":
                self._ptext.append("\n")
            return

        # ── timestamp ────────────────────────────────────────────────────────
        if tag == "time" and "datetime" in a and self._pid is not None:
            try:
                dt = datetime.fromisoformat(a["datetime"] or "")
                self._pts = int(dt.astimezone(timezone.utc).timestamp())
            except (ValueError, TypeError):
                pass

        # ── post permalink ───────────────────────────────────────────────────
        if tag == "a" and "tgme_widget_message_date" in cls and self._pid is not None:
            self._plink = a.get("href")

    def handle_endtag(self, tag: str) -> None:
        if not self._in_text:
            return
        if tag == "div":
            if self._text_depth > 0:
                self._text_depth -= 1
            else:
                # closing the outermost text div — save cleaned text
                self._in_text = False
                cleaned = re.sub(r"[ \t]+", " ", "".join(self._ptext)).strip()
                cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
                self._ptext = [cleaned]

    def handle_data(self, data: str) -> None:
        if self._in_text:
            self._ptext.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    @property
    def posts(self) -> list[ScrapedPost]:
        return self._posts


# ── network ───────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def _fetch_sync(username: str) -> str:
    url = f"https://t.me/s/{username.lstrip('@')}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for t.me/s/{username}") from exc
    except Exception as exc:
        raise RuntimeError(f"Fetch error for {username}: {exc}") from exc


# ── public API ────────────────────────────────────────────────────────────────

async def fetch_channel_posts(username: str, limit: int = 20) -> list[ScrapedPost]:
    """
    Scrape recent posts from a public Telegram channel via t.me/s/.

    Args:
        username: channel @username (with or without @)
        limit:    max posts to return (newest first)

    Returns:
        List of ScrapedPost sorted newest-first.
    """
    loop = asyncio.get_event_loop()
    html = await loop.run_in_executor(None, _fetch_sync, username)
    parser = _TGParser(username)
    parser.feed(html)
    parser.close()
    posts = sorted(parser.posts, key=lambda p: p.ts, reverse=True)
    return posts[:limit]

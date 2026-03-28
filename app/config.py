from __future__ import annotations

from dataclasses import dataclass
import os


def _get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer for {name}: {value}") from exc


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ids.add(_parse_int(part, "ADMIN_IDS"))
    if not ids:
        raise RuntimeError("ADMIN_IDS must contain at least one Telegram user id")
    return ids


@dataclass(frozen=True)
class Config:
    bot_token: str
    openai_api_key: str
    openai_model: str
    timezone: str
    max_items: int
    snippet_chars: int
    db_path: str
    admin_ids: set[int]
    history_limit: int


def load_config() -> Config:
    return Config(
        bot_token=_get_env("TELEGRAM_BOT_TOKEN"),
        openai_api_key=_get_env("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
        max_items=_parse_int(os.getenv("MAX_ITEMS", "10"), "MAX_ITEMS"),
        snippet_chars=_parse_int(os.getenv("SNIPPET_CHARS", "280"), "SNIPPET_CHARS"),
        db_path=os.getenv("DB_PATH", os.path.join("data", "bot.db")),
        admin_ids=_parse_admin_ids(_get_env("ADMIN_IDS")),
        history_limit=_parse_int(os.getenv("HISTORY_LIMIT", "30"), "HISTORY_LIMIT"),
    )

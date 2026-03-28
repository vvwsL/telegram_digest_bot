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
    tg_api_id: int
    tg_api_hash: str
    openai_api_key: str
    openai_model: str
    timezone: str
    max_items_per_topic: int
    snippet_chars: int
    db_path: str
    admin_ids: set[int]
    history_limit: int  # posts to fetch on first run per source


def load_config() -> Config:
    bot_token = _get_env("TELEGRAM_BOT_TOKEN")
    tg_api_id = _parse_int(_get_env("TG_API_ID"), "TG_API_ID")
    tg_api_hash = _get_env("TG_API_HASH")
    openai_api_key = _get_env("OPENAI_API_KEY")
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    timezone = os.getenv("TIMEZONE", "Europe/Moscow")
    max_items = _parse_int(os.getenv("MAX_ITEMS_PER_TOPIC", "10"), "MAX_ITEMS_PER_TOPIC")
    snippet_chars = _parse_int(os.getenv("SNIPPET_CHARS", "280"), "SNIPPET_CHARS")
    db_path = os.getenv("DB_PATH", os.path.join("data", "bot.db"))
    admin_ids = _parse_admin_ids(_get_env("ADMIN_IDS"))
    history_limit = _parse_int(os.getenv("HISTORY_LIMIT", "30"), "HISTORY_LIMIT")
    return Config(
        bot_token=bot_token,
        tg_api_id=tg_api_id,
        tg_api_hash=tg_api_hash,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        timezone=timezone,
        max_items_per_topic=max_items,
        snippet_chars=snippet_chars,
        db_path=db_path,
        admin_ids=admin_ids,
        history_limit=history_limit,
    )

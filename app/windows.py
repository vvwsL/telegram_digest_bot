from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo


DAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "пн": 0,
    "пон": 0,
    "понедельник": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "вт": 1,
    "втр": 1,
    "вторник": 1,
    "wed": 2,
    "wednesday": 2,
    "ср": 2,
    "среда": 2,
    "thu": 3,
    "thur": 3,
    "thursday": 3,
    "чт": 3,
    "четверг": 3,
    "fri": 4,
    "friday": 4,
    "пт": 4,
    "пятница": 4,
    "sat": 5,
    "saturday": 5,
    "сб": 5,
    "суббота": 5,
    "sun": 6,
    "sunday": 6,
    "вс": 6,
    "воскресенье": 6,
}


DAY_NAMES_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


@dataclass(frozen=True)
class Window:
    id: int
    days: list[int]
    start: time
    end: time


def parse_days(raw: str) -> list[int]:
    raw = raw.strip().lower()
    if raw in {"*", "all", "every", "ежедневно", "каждыйдень"}:
        return list(range(7))

    parts: list[str] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            parts.append(chunk)
        else:
            parts.append(chunk)

    days: set[int] = set()
    for part in parts:
        if "-" in part:
            start_raw, end_raw = [p.strip() for p in part.split("-", 1)]
            start_day = _parse_day_token(start_raw)
            end_day = _parse_day_token(end_raw)
            if start_day is None or end_day is None:
                raise ValueError(f"Unknown day token in range: {part}")
            if start_day <= end_day:
                for d in range(start_day, end_day + 1):
                    days.add(d)
            else:
                for d in range(start_day, 7):
                    days.add(d)
                for d in range(0, end_day + 1):
                    days.add(d)
        else:
            day = _parse_day_token(part)
            if day is None:
                raise ValueError(f"Unknown day token: {part}")
            days.add(day)

    if not days:
        raise ValueError("No valid days specified")
    return sorted(days)


def _parse_day_token(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        num = int(token)
        if 0 <= num <= 6:
            return num
        if 1 <= num <= 7:
            return num - 1
        return None
    return DAY_ALIASES.get(token)


def format_days(days: list[int]) -> str:
    return ",".join(DAY_NAMES_RU[d] for d in days)


def parse_time_range(raw: str) -> tuple[time, time]:
    raw = raw.strip()
    match = re.fullmatch(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", raw)
    if not match:
        raise ValueError("Time range must be HH:MM-HH:MM")
    start = _parse_time(match.group(1))
    end = _parse_time(match.group(2))
    if start == end:
        raise ValueError("Start and end time must differ")
    return start, end


def _parse_time(raw: str) -> time:
    hour, minute = raw.split(":")
    hour_i = int(hour)
    minute_i = int(minute)
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        raise ValueError(f"Invalid time: {raw}")
    return time(hour=hour_i, minute=minute_i)


def is_dt_in_window(dt: datetime, days: list[int], start: time, end: time) -> bool:
    current_time = dt.time()
    weekday = dt.weekday()
    if start < end:
        return weekday in days and start <= current_time < end
    if current_time >= start:
        return weekday in days
    prev_day = (weekday - 1) % 7
    return prev_day in days and current_time < end


def window_end_for_now(
    now: datetime, days: list[int], start: time, end: time, tz: ZoneInfo
) -> tuple[datetime, datetime] | None:
    if now.hour != end.hour or now.minute != end.minute:
        return None

    if start < end:
        if now.weekday() not in days:
            return None
        start_dt = datetime.combine(date=now.date(), time=start, tzinfo=tz)
        end_dt = datetime.combine(date=now.date(), time=end, tzinfo=tz)
        return start_dt, end_dt

    prev_day = (now.weekday() - 1) % 7
    if prev_day not in days:
        return None
    start_dt = datetime.combine(date=now.date() - timedelta(days=1), time=start, tzinfo=tz)
    end_dt = datetime.combine(date=now.date(), time=end, tzinfo=tz)
    return start_dt, end_dt

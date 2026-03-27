from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from openai import OpenAI


@dataclass(frozen=True)
class DigestItem:
    snippet: str
    source: str
    link: str | None
    important: bool


class Summarizer:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model

    async def summarize(
        self,
        topic: str,
        start_dt: datetime,
        end_dt: datetime,
        items: Iterable[DigestItem],
    ) -> str:
        system = (
            "Ты редактор дайджеста. Пиши по-русски, кратко и без выдумок. "
            "Используй только факты из списка. "
            "Формат: заголовок с темой и датами, затем пункты с источниками и ссылками. "
            "Если пункт помечен как важный, подчеркни это словом 'Важно:'."
        )
        lines = [
            f"Тема: {topic}",
            f"Период: {start_dt.strftime('%d.%m.%Y %H:%M')} – {end_dt.strftime('%d.%m.%Y %H:%M')}",
            "Пункты:",
        ]
        for idx, item in enumerate(items, start=1):
            link_part = f" | {item.link}" if item.link else ""
            important = "IMPORTANT" if item.important else "NORMAL"
            lines.append(f"{idx}) [{important}] {item.snippet} | {item.source}{link_part}")
        user = "\n".join(lines)

        def _call() -> str:
            response = self._client.responses.create(
                model=self._model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            text = getattr(response, "output_text", None)
            if text:
                return text.strip()
            # Fallback for SDK variations
            try:
                for output in response.output:
                    for content in getattr(output, "content", []):
                        if getattr(content, "type", "") in {"output_text", "text"}:
                            return content.text.strip()
            except Exception:
                pass
            return ""

        return await asyncio.to_thread(_call)

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


@dataclass(frozen=True)
class SummaryResult:
    text: str
    tokens_in: int
    tokens_out: int

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out


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
    ) -> SummaryResult:
        system = (
            "Ты редактор дайджеста. Пиши по-русски, кратко и без выдумок. "
            "Используй только факты из списка. "
            "Формат: заголовок с темой и датами, затем пункты с источниками и ссылками. "
            "Если пункт помечен IMPORTANT — добавь 'Важно:' перед ним. "
            "Если несколько каналов сообщают об одном и том же событии — "
            "объедини в один пункт, укажи все источники через запятую. "
            "Не дублируй одну и ту же новость."
        )
        lines = [
            f"Тема: {topic}",
            f"Период: {start_dt.strftime('%d.%m %H:%M')}–{end_dt.strftime('%d.%m %H:%M')}",
            "Пункты:",
        ]
        for idx, item in enumerate(items, start=1):
            link_part = f" {item.link}" if item.link else ""
            flag = "IMPORTANT" if item.important else "NORMAL"
            lines.append(f"{idx}. [{flag}] {item.snippet} | {item.source}{link_part}")
        user_msg = "\n".join(lines)

        def _call() -> SummaryResult:
            response = self._client.responses.create(
                model=self._model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
            )
            # Extract text
            text = ""
            text_attr = getattr(response, "output_text", None)
            if text_attr:
                text = text_attr.strip()
            else:
                try:
                    for output in response.output:
                        for content in getattr(output, "content", []):
                            if getattr(content, "type", "") in {"output_text", "text"}:
                                text = content.text.strip()
                                break
                except Exception:
                    pass

            # Extract token usage
            tokens_in, tokens_out = 0, 0
            usage = getattr(response, "usage", None)
            if usage:
                tokens_in = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0)
                tokens_out = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0)

            return SummaryResult(text=text, tokens_in=tokens_in, tokens_out=tokens_out)

        return await asyncio.to_thread(_call)

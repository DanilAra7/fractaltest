"""Вузький інтерфейс до LLM. Зміна провайдера = один новий файл у цьому пакеті."""

from __future__ import annotations

from typing import Protocol


class LLMError(RuntimeError):
    """Помилка на боці провайдера (мережа, ліміти, порожня відповідь)."""


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, system: str, user: str, schema: dict) -> str:
        """Повертає сирий текст відповіді (очікується JSON). Валідація — вище."""
        ...

"""Вибір провайдера. Додати новий = один файл тут плюс рядок у get_provider."""

from __future__ import annotations

from .base import LLMError, LLMProvider
from .mock import MockProvider

__all__ = ["LLMError", "LLMProvider", "MockProvider", "get_provider"]


def get_provider(name: str, model: str | None = None, temperature: float = 0.0):
    if name == "mock":
        return MockProvider(model=model or "mock-fixtures-v1", temperature=temperature)
    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(model=model, temperature=temperature)
    raise LLMError(f"Невідомий провайдер: {name}. Доступні: mock, gemini")

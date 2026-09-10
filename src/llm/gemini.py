"""Gemini через google-genai. Схема віддається моделі як response_schema,
щоб форму гарантував сам декодер, а не парсер після факту."""

from __future__ import annotations

import asyncio
import os
import random

from .base import LLMError

DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        model: str | None = None,
        temperature: float = 0.0,
        max_retries: int = 4,
    ) -> None:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "Не встановлено google-genai. Постав: pip install google-genai"
            ) from exc

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise LLMError("GEMINI_API_KEY не заданий. Див. .env.example")

        self._client = genai.Client(api_key=api_key)
        self.model = model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
        self.temperature = temperature
        self.max_retries = max_retries

    async def generate(self, system: str, user: str, schema: dict) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self.model, contents=user, config=config
                )
                text = (response.text or "").strip()
                if not text:
                    raise LLMError("Порожня відповідь від моделі")
                return text
            except Exception as exc:  # rate limit, 5xx, обрив мережі
                last_exc = exc
                if attempt == self.max_retries - 1:
                    break
                # Експоненційний backoff з джитером: безкоштовний тір Gemini
                # обмежений по RPM і 429 тут очікувана, а не виняткова подія.
                delay = (2**attempt) + random.uniform(0, 1)
                await asyncio.sleep(delay)

        raise LLMError(f"Gemini не відповів за {self.max_retries} спроб: {last_exc}")

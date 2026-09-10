"""Gemini через google-genai. Схема віддається моделі як response_schema,
щоб форму гарантував сам декодер, а не парсер після факту."""

from __future__ import annotations

import asyncio
import logging
import os
import random

from .base import LLMError

# SDK попереджає про automatic function calling при кожному виклику — ми
# tools не передаємо, попередження нерелевантне й лише засмічує вивід.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

DEFAULT_MODEL = "gemini-3.6-flash"

# Ключі, які pydantic кладе в JSON Schema, але яких немає в урізаному
# OpenAPI-підмножині, що приймає Gemini response_schema.
_UNSUPPORTED_KEYS = {"title", "additionalProperties", "default"}


def to_gemini_schema(schema: dict, defs: dict | None = None) -> dict:
    """Конвертує JSON Schema (pydantic) у формат response_schema Gemini.

    Gemini не розуміє $ref/$defs, additionalProperties і anyOf — усе це
    pydantic генерує за замовчуванням. Без конвертації API повертає
    400 INVALID_ARGUMENT ще до першого токена генерації.
    """
    defs = defs if defs is not None else schema.get("$defs", {})

    if "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        return to_gemini_schema(defs[ref_name], defs)

    # anyOf [{type: X}, {type: "null"}] (Optional[X] у pydantic) -> nullable.
    if "anyOf" in schema:
        variants = [to_gemini_schema(v, defs) for v in schema["anyOf"]]
        non_null = [v for v in variants if v.get("type") != "null"]
        has_null = len(non_null) != len(variants)
        if len(non_null) == 1:
            result = dict(non_null[0])
            if has_null:
                result["nullable"] = True
            return result
        # Справжній anyOf з кількох типів Gemini не підтримує — беремо перший
        # непорожній варіант, це не наш випадок (тут лише Optional[str]).
        result = dict(non_null[0]) if non_null else {"type": "string"}
        if has_null:
            result["nullable"] = True
        return result

    result: dict = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_KEYS or key == "$defs":
            continue
        if key == "properties":
            result[key] = {k: to_gemini_schema(v, defs) for k, v in value.items()}
        elif key == "items":
            result[key] = to_gemini_schema(value, defs)
        else:
            result[key] = value

    if result.get("type") == "object" and "properties" in result:
        result.setdefault("propertyOrdering", list(result["properties"].keys()))

    return result


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
            response_schema=to_gemini_schema(schema),
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

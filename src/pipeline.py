"""Асинхронна обробка інбоксу: LLM → валідація → (за потреби) ремонт."""

from __future__ import annotations

import asyncio
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .cache import ResponseCache
from .llm import LLMError
from .models import (
    InboxRequest,
    ProcessedRequest,
    RequestAnalysis,
    RunMetadata,
    RunResult,
)
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_repair_prompt, build_user_prompt

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def read_inbox(path: Path) -> list[InboxRequest]:
    """Читає CSV, пропускаючи рядки, які взагалі не є запитом."""
    requests: list[InboxRequest] = []
    skipped: list[str] = []

    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            try:
                requests.append(InboxRequest.model_validate(row))
            except ValidationError as exc:
                skipped.append(f"рядок {row_number}: {exc.errors()[0]['msg']}")

    if skipped:
        print(f"[warn] пропущено рядків CSV: {len(skipped)}")
        for note in skipped:
            print(f"       {note}")

    return requests


def extract_json(text: str) -> dict:
    """Дістає JSON-об'єкт з відповіді моделі.

    Structured output у реального провайдера робить це зайвим, але мок і будь-яка
    модель без constrained decoding люблять обгортати JSON у ```json та пояснення.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(cleaned)
        if not match:
            raise ValueError("У відповіді немає JSON-об'єкта")
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError(f"Очікувався JSON-об'єкт, отримано {type(parsed).__name__}")
    return parsed


def format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"{location}: {err['msg']}")
    return "; ".join(lines)


class TriagePipeline:
    def __init__(
        self,
        provider,
        cache: ResponseCache,
        concurrency: int = 5,
        max_repair_attempts: int = 1,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_repair_attempts = max_repair_attempts
        self.schema = RequestAnalysis.model_json_schema()

    async def process_one(self, request: InboxRequest) -> ProcessedRequest:
        async with self.semaphore:
            return await self._process(request)

    async def _process(self, request: InboxRequest) -> ProcessedRequest:
        base = {
            "id": request.id,
            "channel": request.channel,
            "timestamp": request.timestamp,
            "raw_text": request.raw_text,
        }

        cache_key = ResponseCache.make_key(
            request.raw_text, PROMPT_VERSION, self.provider.model
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            try:
                return ProcessedRequest(
                    **base,
                    status="ok",
                    attempts=0,
                    analysis=RequestAnalysis.model_validate(cached),
                    from_cache=True,
                )
            except ValidationError:
                # Кеш зі старої, несумісної схеми — просто ігноруємо його.
                pass

        user_prompt = build_user_prompt(request)
        attempts = 0
        last_output = ""
        last_error = ""

        # Спроба №1 + до max_repair_attempts спроб ремонту: у ремонтний промпт
        # іде текст помилки валідації, щоб модель правила саме те, що зламано.
        for attempt in range(self.max_repair_attempts + 1):
            attempts += 1
            prompt = user_prompt
            if attempt > 0:
                prompt = f"{user_prompt}\n\n{build_repair_prompt(last_output, last_error)}"

            try:
                last_output = await self.provider.generate(
                    SYSTEM_PROMPT, prompt, self.schema
                )
            except LLMError as exc:
                last_error = f"llm_error: {exc}"
                break

            try:
                analysis = RequestAnalysis.model_validate(extract_json(last_output))
            except ValidationError as exc:
                last_error = f"validation_error: {format_validation_error(exc)}"
                continue
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = f"parse_error: {exc}"
                continue

            self.cache.set(cache_key, analysis.model_dump(mode="json"))
            return ProcessedRequest(
                **base, status="ok", attempts=attempts, analysis=analysis
            )

        # Вичерпали спроби: запит не губиться, а лишається в output.json
        # зі статусом failed і текстом останньої помилки.
        return ProcessedRequest(
            **base, status="failed", attempts=attempts, analysis=None, error=last_error
        )

    async def run(self, requests: list[InboxRequest]) -> RunResult:
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc)

        results = await asyncio.gather(*(self.process_one(r) for r in requests))
        results = sorted(results, key=lambda r: r.id)

        finished_at = datetime.now(timezone.utc)
        metadata = RunMetadata(
            provider=self.provider.name,
            model=self.provider.model,
            prompt_version=PROMPT_VERSION,
            temperature=getattr(self.provider, "temperature", 0.0),
            started_at=started_at.isoformat(timespec="seconds"),
            finished_at=finished_at.isoformat(timespec="seconds"),
            duration_seconds=round(time.perf_counter() - started, 2),
            total_requests=len(results),
            ok=sum(1 for r in results if r.status == "ok"),
            failed=sum(1 for r in results if r.status == "failed"),
        )
        return RunResult(metadata=metadata, results=results)

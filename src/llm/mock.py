"""Мок-провайдер: детермінований прогін усього пайплайну без API-ключа.

Він навмисно поводиться як реальна модель у найнеприємніші моменти:
на частині запитів перша спроба повертає зламаний JSON або значення поза
enum, і полагодити це має шар валідації, а не мок.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from .base import LLMError

FIXTURES_PATH = Path(__file__).with_name("mock_fixtures.json")

# Скільки "думає" мок — щоб async-обробка мала що паралелити.
LATENCY_SECONDS = 0.15

_REQUEST_RE = re.compile(r"<request>\s*(.*?)\s*</request>", re.DOTALL)


class MockProvider:
    """Підбирає відповідь за характерним фрагментом тексту запиту."""

    name = "mock"

    def __init__(self, model: str = "mock-fixtures-v1", temperature: float = 0.0):
        self.model = model
        self.temperature = temperature
        self._fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
        # Скільки разів кожен маркер уже питали в межах прогону: потрібно,
        # щоб одноразові збої спрацьовували саме на першій спробі.
        self._calls: dict[str, int] = {}

    async def generate(self, system: str, user: str, schema: dict) -> str:
        await asyncio.sleep(LATENCY_SECONDS)

        match = _REQUEST_RE.search(user)
        raw_text = match.group(1) if match else user
        fixture = self._match(raw_text)

        if fixture is None:
            raise LLMError(
                "Мок не має фікстури для цього тексту. Мок працює лише на "
                "data/input_requests.csv; для інших даних потрібен реальний провайдер."
            )

        marker = fixture["marker"]
        call_index = self._calls.get(marker, 0)
        self._calls[marker] = call_index + 1

        return self._apply_behavior(fixture, call_index)

    def _match(self, raw_text: str) -> dict | None:
        haystack = raw_text.lower()
        matches = [f for f in self._fixtures if f["marker"].lower() in haystack]
        if len(matches) > 1:
            # Неоднозначний маркер — це помилка фікстур, а не даних. Падаємо
            # голосно, інакше запит тихо отримає чужу відповідь.
            markers = ", ".join(repr(m["marker"]) for m in matches)
            raise LLMError(f"Текст підпадає під кілька фікстур одразу: {markers}")
        return matches[0] if matches else None

    def _apply_behavior(self, fixture: dict, call_index: int) -> str:
        analysis = fixture["analysis"]
        behavior = fixture.get("behavior")
        first_call = call_index == 0

        if behavior == "broken_json_once" and first_call:
            # Класика: модель обгортає JSON у markdown і обриває його на півслові.
            return "```json\n" + json.dumps(analysis, ensure_ascii=False)[:120]

        if behavior == "invalid_enum_once" and first_call:
            broken = dict(analysis, category="незрозуміло", confidence=1.4)
            return json.dumps(broken, ensure_ascii=False)

        if behavior == "always_broken":
            return json.dumps(
                {"category": "автоматизація", "priority": "urgent"},
                ensure_ascii=False,
            )

        return json.dumps(analysis, ensure_ascii=False, indent=2)

"""Тести на шар валідації: саме він тримає удар кривого виводу LLM."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from src.cache import ResponseCache
from src.llm.mock import MockProvider
from src.models import Category, InboxRequest, RequestAnalysis
from src.pipeline import TriagePipeline, extract_json

VALID = {
    "category": "автоматизація",
    "target_department": "маркетинг",
    "priority": "medium",
    "short_summary": "Автоматизувати щотижневий звіт.",
    "requested_actions": ["Зробити звіт"],
    "needs_clarification": False,
    "clarifying_questions": [],
    "priority_rationale": "Звичайна задача без дедлайну.",
    "deadline_mentioned": None,
    "confidence": 0.8,
    "is_multi_request": False,
    "language": "uk",
}


def analysis(**overrides) -> dict:
    return {**VALID, **overrides}


class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_prose_around_it(self):
        assert extract_json('Ось відповідь:\n{"a": 1}\nСподіваюсь, підійде.') == {"a": 1}

    def test_truncated_json_raises(self):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            extract_json('{"a": 1, "b":')

    def test_array_is_not_an_object(self):
        with pytest.raises(ValueError):
            extract_json("[1, 2, 3]")


class TestSchema:
    def test_valid_payload(self):
        assert RequestAnalysis.model_validate(VALID).category is Category.AUTOMATION

    def test_unknown_category_rejected(self):
        with pytest.raises(ValidationError):
            RequestAnalysis.model_validate(analysis(category="незрозуміло"))

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            RequestAnalysis.model_validate(analysis(confidence=1.4))

    def test_invented_department_becomes_none(self):
        # Модель вигадала відділ, якого немає — краще null, ніж вигадка.
        got = RequestAnalysis.model_validate(analysis(target_department="Відділ магії"))
        assert got.target_department is None

    def test_department_aliases_normalized(self):
        for alias in ("Sales", "продажи", " ПРОДАЖІ "):
            got = RequestAnalysis.model_validate(analysis(target_department=alias))
            assert got.target_department == "продажі"

    def test_ready_request_must_have_actions(self):
        with pytest.raises(ValidationError):
            RequestAnalysis.model_validate(
                analysis(needs_clarification=False, requested_actions=[])
            )

    def test_out_of_scope_may_have_no_actions(self):
        got = RequestAnalysis.model_validate(
            analysis(
                category="поза скоупом", needs_clarification=False, requested_actions=[]
            )
        )
        assert got.requested_actions == []

    def test_clarification_requires_questions(self):
        with pytest.raises(ValidationError):
            RequestAnalysis.model_validate(
                analysis(needs_clarification=True, clarifying_questions=[])
            )

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            RequestAnalysis.model_validate(analysis(sentiment="positive"))

    def test_empty_strings_dropped_from_lists(self):
        got = RequestAnalysis.model_validate(
            analysis(requested_actions=["  Зробити звіт  ", "", "   "])
        )
        assert got.requested_actions == ["Зробити звіт"]

    def test_empty_raw_text_rejected(self):
        with pytest.raises(ValidationError):
            InboxRequest(id="X", channel="Slack", timestamp="t", raw_text="   ")


class TestRepairLoop:
    """Мок віддає биту відповідь на першій спробі — пайплайн має її полагодити."""

    @staticmethod
    def _pipeline(tmp_path, repair_attempts=1):
        return TriagePipeline(
            provider=MockProvider(),
            cache=ResponseCache(tmp_path / "cache", enabled=False),
            concurrency=2,
            max_repair_attempts=repair_attempts,
        )

    def test_broken_json_is_repaired(self, tmp_path):
        request = InboxRequest(
            id="REQ-002", channel="Telegram", timestamp="t", raw_text="хлопці треба бот"
        )
        got = asyncio.run(self._pipeline(tmp_path).process_one(request))
        assert got.status == "ok"
        assert got.attempts == 2
        assert got.analysis.needs_clarification is True

    def test_invalid_enum_is_repaired(self, tmp_path):
        request = InboxRequest(
            id="REQ-011",
            channel="Telegram",
            timestamp="t",
            raw_text="нам би табличку якусь",
        )
        got = asyncio.run(self._pipeline(tmp_path).process_one(request))
        assert got.status == "ok"
        assert got.attempts == 2

    def test_no_repair_budget_means_failed_not_crash(self, tmp_path):
        request = InboxRequest(
            id="REQ-002", channel="Telegram", timestamp="t", raw_text="хлопці треба бот"
        )
        got = asyncio.run(
            self._pipeline(tmp_path, repair_attempts=0).process_one(request)
        )
        assert got.status == "failed"
        assert got.analysis is None
        assert "parse_error" in got.error

    def test_unknown_text_fails_gracefully(self, tmp_path):
        request = InboxRequest(
            id="REQ-999", channel="Slack", timestamp="t", raw_text="щось геть інше"
        )
        got = asyncio.run(self._pipeline(tmp_path).process_one(request))
        assert got.status == "failed"
        assert "llm_error" in got.error


class TestCache:
    def test_key_changes_with_prompt_version(self):
        a = ResponseCache.make_key("текст", "v1", "m")
        b = ResponseCache.make_key("текст", "v2", "m")
        assert a != b

    def test_roundtrip(self, tmp_path):
        cache = ResponseCache(tmp_path, enabled=True)
        cache.set("k", VALID)
        assert cache.get("k") == VALID

    def test_corrupt_entry_returns_none(self, tmp_path):
        cache = ResponseCache(tmp_path, enabled=True)
        (tmp_path / "k.json").write_text("{не json", encoding="utf-8")
        assert cache.get("k") is None

"""Тести логіки бота. Мережі тут немає: build_reply() приймає `analyze`
корутиною, тому підставляємо заглушку замість справжнього Gemini."""

from __future__ import annotations

import asyncio

import pytest

from src.bot import (
    GREETING,
    MAX_INPUT_CHARS,
    MIN_SECONDS_BETWEEN_REQUESTS,
    UNLOCKED,
    WRONG_PASSWORD,
    BotState,
    build_reply,
    format_analysis,
    format_failure,
)
from src.models import ProcessedRequest, RequestAnalysis

PASSWORD = "netpeak2026"


@pytest.fixture(autouse=True)
def _set_password(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ACCESS_WORD", PASSWORD)


def analysis(**overrides) -> RequestAnalysis:
    base = dict(
        category="автоматизація", target_department="маркетинг", priority="medium",
        short_summary="Автоматизувати щотижневий звіт.",
        requested_actions=["Зробити звіт"], needs_clarification=False,
        clarifying_questions=[], priority_rationale="Рутина без дедлайну.",
        deadline_mentioned=None, confidence=0.8, is_multi_request=False, language="uk",
    )
    base.update(overrides)
    return RequestAnalysis.model_validate(base)


def ok_result(**overrides) -> ProcessedRequest:
    return ProcessedRequest(
        id="telegram", channel="Telegram", timestamp="2026-09-10 12:00",
        raw_text="текст", status="ok", attempts=1, analysis=analysis(**overrides),
    )


def failed_result(error: str) -> ProcessedRequest:
    return ProcessedRequest(
        id="telegram", channel="Telegram", timestamp="2026-09-10 12:00",
        raw_text="текст", status="failed", attempts=2, analysis=None, error=error,
    )


def reply(state, chat_id, text, result=None):
    async def analyze(_text):
        return result if result is not None else ok_result()

    return asyncio.run(build_reply(state, chat_id, text, analyze))


class TestAccessGate:
    def test_start_before_password_asks_for_it(self):
        assert reply(BotState(), 1, "/start") == GREETING

    def test_random_text_before_password_is_rejected(self):
        state = BotState()
        assert reply(state, 1, "привіт, зроби мені звіт") == WRONG_PASSWORD
        assert not state.is_authorized(1)

    def test_correct_password_unlocks(self):
        state = BotState()
        assert reply(state, 1, PASSWORD) == UNLOCKED
        assert state.is_authorized(1)

    def test_password_is_case_insensitive_and_trimmed(self):
        state = BotState()
        assert reply(state, 1, f"  {PASSWORD.upper()}  ") == UNLOCKED

    def test_authorization_is_per_chat(self):
        state = BotState()
        reply(state, 1, PASSWORD)
        assert state.is_authorized(1)
        # Інший чат не успадковує доступ.
        assert not state.is_authorized(2)
        assert reply(state, 2, "дай розбір") == WRONG_PASSWORD

    def test_no_password_configured_blocks_everyone(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_ACCESS_WORD", raising=False)
        state = BotState()
        assert reply(state, 1, PASSWORD) == WRONG_PASSWORD


class TestTriageReply:
    def _authorized(self):
        state = BotState()
        reply(state, 1, PASSWORD)
        return state

    def test_returns_structured_analysis(self):
        out = reply(self._authorized(), 1, "автоматизуйте звіт по Google Ads")
        assert "Категорія: автоматизація" in out
        assert "Пріоритет: medium" in out
        assert "Відділ: маркетинг" in out
        assert "Зробити звіт" in out

    def test_clarification_block_shown(self):
        result = ok_result(
            needs_clarification=True, requested_actions=[],
            clarifying_questions=["Який саме бот?"],
        )
        out = reply(self._authorized(), 1, "треба бот", result)
        assert "надто розмитий" in out
        assert "Який саме бот?" in out

    def test_quota_error_explained_in_human_terms(self):
        result = failed_result("llm_error: 429 RESOURCE_EXHAUSTED ...")
        out = reply(self._authorized(), 1, "запит", result)
        assert "ліміт" in out.lower()
        assert "429" not in out

    def test_other_errors_surface_the_reason(self):
        out = reply(self._authorized(), 1, "запит", failed_result("parse_error: зламано"))
        assert "parse_error" in out

    def test_too_long_input_rejected_without_calling_llm(self):
        called = False

        async def analyze(_text):
            nonlocal called
            called = True
            return ok_result()

        state = self._authorized()
        out = asyncio.run(build_reply(state, 1, "я" * (MAX_INPUT_CHARS + 1), analyze))
        assert "Задовгий" in out
        assert not called


class TestRateLimit:
    def test_second_immediate_request_is_throttled(self):
        state = BotState()
        reply(state, 1, PASSWORD)
        assert "Категорія" in reply(state, 1, "перший запит")
        assert "Занадто часто" in reply(state, 1, "другий запит одразу")

    def test_throttle_expires(self):
        state = BotState()
        state.authorize(1)
        state.mark_request(1, now=0.0)
        assert state.seconds_until_allowed(1, now=1.0) == pytest.approx(
            MIN_SECONDS_BETWEEN_REQUESTS - 1
        )
        assert state.seconds_until_allowed(1, now=MIN_SECONDS_BETWEEN_REQUESTS + 1) == 0.0

    def test_throttle_is_per_chat(self):
        state = BotState()
        state.authorize(1)
        state.authorize(2)
        state.mark_request(1, now=0.0)
        assert state.seconds_until_allowed(2, now=0.0) == 0.0


class TestFormatting:
    def test_deadline_and_multi_flags_rendered(self):
        out = format_analysis(
            analysis(deadline_mentioned="сьогодні до вечора", is_multi_request=True)
        )
        assert "сьогодні до вечора" in out
        assert "кілька задач" in out

    def test_confidence_as_percent(self):
        assert "80%" in format_analysis(analysis(confidence=0.8))

    def test_missing_department_says_so(self):
        assert "не визначено" in format_analysis(analysis(target_department=None))

    def test_failure_without_error_text(self):
        result = failed_result("")
        result.error = None
        assert "невідома помилка" in format_failure(result)


class TestIgnoredMessages:
    def test_empty_text_gets_no_reply(self):
        assert reply(BotState(), 1, "   ") is None

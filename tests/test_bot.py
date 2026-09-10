"""Тести логіки бота. Мережі тут немає: build_reply() приймає `analyze`
корутиною, тому підставляємо заглушку замість справжнього Gemini."""

from __future__ import annotations

import asyncio

import pytest

from src.bot import (
    GREETING,
    MAX_INPUT_CHARS,
    MIN_SECONDS_BETWEEN_REQUESTS,
    READY_FOR_INPUT,
    REPORT_MISSING,
    UNLOCKED_INTRO,
    WRONG_PASSWORD,
    BotState,
    build_reply,
    format_analysis,
    format_failure,
    load_report_digest,
)
from src.models import ProcessedRequest, RequestAnalysis

PASSWORD = "netpeak2026"

SAMPLE_RUN_JSON = """{
  "metadata": {
    "provider": "gemini", "model": "gemini-3.6-flash", "prompt_version": "v1",
    "temperature": 0.0, "started_at": "t0", "finished_at": "t1",
    "duration_seconds": 5.9, "total_requests": 1, "ok": 1, "failed": 0
  },
  "results": [{
    "id": "REQ-001", "channel": "Slack", "timestamp": "2026-06-08 09:14",
    "raw_text": "текст", "status": "ok", "attempts": 1, "from_cache": false,
    "error": null,
    "analysis": {
      "category": "автоматизація", "target_department": "маркетинг",
      "priority": "high", "short_summary": "Підсумок.",
      "requested_actions": ["Дія"], "needs_clarification": false,
      "clarifying_questions": [], "priority_rationale": "Причина.",
      "deadline_mentioned": null, "confidence": 0.9,
      "is_multi_request": false, "language": "uk"
    }
  }]
}"""


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


def parts(state, chat_id, text, result=None) -> list[str]:
    """Список повідомлень, які бот надіслав би у відповідь."""

    async def analyze(_text):
        return result if result is not None else ok_result()

    return asyncio.run(build_reply(state, chat_id, text, analyze))


def reply(state, chat_id, text, result=None) -> str:
    """Ті самі повідомлення, склеєні — зручно для перевірок 'містить'."""
    return "\n\n".join(parts(state, chat_id, text, result))


class TestAccessGate:
    def test_start_before_password_asks_for_it(self):
        assert reply(BotState(), 1, "/start") == GREETING

    def test_random_text_before_password_is_rejected(self):
        state = BotState()
        assert reply(state, 1, "привіт, зроби мені звіт") == WRONG_PASSWORD
        assert not state.is_authorized(1)

    def test_correct_password_unlocks(self):
        state = BotState()
        assert UNLOCKED_INTRO in reply(state, 1, PASSWORD)
        assert state.is_authorized(1)

    def test_password_is_case_insensitive_and_trimmed(self):
        state = BotState()
        assert UNLOCKED_INTRO in reply(state, 1, f"  {PASSWORD.upper()}  ")

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
        assert "Задовгий" in "".join(out)
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
        assert parts(BotState(), 1, "   ") == []


class TestReportOnUnlock:
    """Головна вимога ТЗ — звіт по датасету — має приходити першою справою,
    ще до того, як бот перейде в режим 'надсилай свій запит'."""

    def test_unlock_sends_intro_report_and_invitation(self, tmp_path, monkeypatch):
        report = tmp_path / "output.json"
        report.write_text(SAMPLE_RUN_JSON, encoding="utf-8")
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(report))

        messages = parts(BotState(), 1, PASSWORD)
        assert len(messages) == 3
        assert messages[0] == UNLOCKED_INTRO
        assert "Тріаж інбоксу" in messages[1]
        assert messages[2] == READY_FOR_INPUT

    def test_report_precedes_invitation(self, tmp_path, monkeypatch):
        report = tmp_path / "output.json"
        report.write_text(SAMPLE_RUN_JSON, encoding="utf-8")
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(report))

        messages = parts(BotState(), 1, PASSWORD)
        assert messages.index(READY_FOR_INPUT) == 2

    def test_missing_report_does_not_break_unlock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(tmp_path / "немає.json"))
        messages = parts(BotState(), 1, PASSWORD)
        assert messages[1] == REPORT_MISSING
        # Доступ усе одно видано — відсутній файл не має блокувати бота.
        assert messages[2] == READY_FOR_INPUT

    def test_broken_report_file_does_not_crash(self, tmp_path, monkeypatch):
        report = tmp_path / "output.json"
        report.write_text("{не json", encoding="utf-8")
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(report))
        assert load_report_digest() is None

    def test_report_command_resends_it(self, tmp_path, monkeypatch):
        report = tmp_path / "output.json"
        report.write_text(SAMPLE_RUN_JSON, encoding="utf-8")
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(report))

        state = BotState()
        state.authorize(1)
        messages = parts(state, 1, "/report")
        assert len(messages) == 1
        assert "Тріаж інбоксу" in messages[0]

    def test_report_command_needs_authorization(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(tmp_path / "output.json"))
        assert parts(BotState(), 1, "/report") == [WRONG_PASSWORD]

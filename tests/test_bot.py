"""Тести логіки бота. Мережі тут немає: build_reply() приймає `analyze`
корутиною, тому підставляємо заглушку замість справжнього Gemini."""

from __future__ import annotations

import asyncio
import json

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
    Doc,
    build_multipart,
    build_reply,
    format_analysis,
    format_failure,
    load_report_digest,
    load_report_files,
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


def texts(state, chat_id, text, result=None) -> list[str]:
    """Тільки текстові повідомлення, без вкладень."""
    return [p for p in parts(state, chat_id, text, result) if isinstance(p, str)]


def docs(state, chat_id, text, result=None) -> list[Doc]:
    """Тільки вкладення."""
    return [p for p in parts(state, chat_id, text, result) if isinstance(p, Doc)]


def reply(state, chat_id, text, result=None) -> str:
    """Ті самі текстові повідомлення, склеєні — зручно для перевірок 'містить'."""
    return "\n\n".join(texts(state, chat_id, text, result))


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

        messages = texts(BotState(), 1, PASSWORD)
        assert messages[0] == UNLOCKED_INTRO
        assert "Тріаж інбоксу" in messages[1]
        assert messages[-1] == READY_FOR_INPUT

    def test_report_precedes_invitation(self, tmp_path, monkeypatch):
        report = tmp_path / "output.json"
        report.write_text(SAMPLE_RUN_JSON, encoding="utf-8")
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(report))

        messages = texts(BotState(), 1, PASSWORD)
        # Звіт має передувати запрошенню надсилати свої запити.
        assert messages.index(READY_FOR_INPUT) > 1

    def test_missing_report_does_not_break_unlock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(tmp_path / "немає.json"))
        messages = texts(BotState(), 1, PASSWORD)
        assert messages[1] == REPORT_MISSING
        # Доступ усе одно видано — відсутній файл не має блокувати бота.
        assert messages[-1] == READY_FOR_INPUT

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
        messages = texts(state, 1, "/report")
        assert "Тріаж інбоксу" in messages[0]

    def test_report_command_needs_authorization(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(tmp_path / "output.json"))
        assert texts(BotState(), 1, "/report") == [WRONG_PASSWORD]


class TestReportAttachments:
    """ТЗ вимагає два артефакти — структурований вивід і короткий звіт.
    Бот має віддавати обидва, а не тільки текстовий переказ агрегатів."""

    @staticmethod
    def _prepare(tmp_path, monkeypatch, with_files=True):
        report = tmp_path / "output.json"
        report.write_text(SAMPLE_RUN_JSON, encoding="utf-8")
        if with_files:
            (tmp_path / "report.md").write_text("# Звіт\n\nагрегати", encoding="utf-8")
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(report))
        return report

    def test_unlock_attaches_both_artifacts(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        attached = docs(BotState(), 1, PASSWORD)
        assert [d.filename for d in attached] == ["output.json", "report.md"]

    def test_attached_json_is_the_real_run_output(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        attached = docs(BotState(), 1, PASSWORD)
        payload = json.loads(attached[0].content.decode("utf-8"))
        assert payload["metadata"]["total_requests"] == 1
        assert payload["results"][0]["id"] == "REQ-001"

    def test_missing_report_md_does_not_block_the_rest(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch, with_files=False)
        attached = docs(BotState(), 1, PASSWORD)
        assert [d.filename for d in attached] == ["output.json"]

    def test_report_command_also_attaches(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        state = BotState()
        state.authorize(1)
        assert len(docs(state, 1, "/report")) == 2

    def test_no_files_when_report_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPORT_FILE", str(tmp_path / "немає.json"))
        assert load_report_files() == []


class TestStructuredOutputForSingleRequest:
    def test_reply_includes_valid_json_of_the_analysis(self):
        state = BotState()
        state.authorize(1)
        messages = texts(state, 1, "автоматизуйте звіт")
        assert len(messages) == 2
        payload = json.loads(messages[1].split("\n\n", 1)[1])
        # Рівно та сама схема, що лягає в output.json.
        assert payload["category"] == "автоматизація"
        assert payload["priority"] == "medium"
        assert payload["requested_actions"] == ["Зробити звіт"]

    def test_failure_has_no_json_block(self):
        state = BotState()
        state.authorize(1)
        messages = texts(state, 1, "запит", failed_result("parse_error: зламано"))
        assert len(messages) == 1


class TestMultipart:
    def test_contains_fields_file_and_terminator(self):
        body, content_type = build_multipart({"chat_id": "42"}, "out.json", b'{"a":1}')
        assert "multipart/form-data; boundary=" in content_type
        boundary = content_type.split("boundary=")[1]
        assert f'name="chat_id"'.encode() in body
        assert b"42" in body
        assert b'filename="out.json"' in body
        assert b'{"a":1}' in body
        assert body.endswith(f"\r\n--{boundary}--\r\n".encode())

    def test_binary_content_survives_intact(self):
        blob = bytes(range(256))
        body, _ = build_multipart({"chat_id": "1"}, "f.bin", blob)
        assert blob in body

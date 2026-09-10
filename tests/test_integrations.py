"""Тести опціональних інтеграцій. Мережевого коду тут навмисно немає —
build_digest_text() чиста і тестується без реального Telegram/Google API."""

from __future__ import annotations

import pytest

from src.integrations.base import IntegrationError
from src.integrations.sheets import SHEET_COLUMNS, _row, write_to_google_sheet
from src.integrations.telegram import TELEGRAM_MAX_LEN, build_digest_text
from src.models import (
    ProcessedRequest,
    RequestAnalysis,
    RunMetadata,
    RunResult,
)


def make_result(rows: list[ProcessedRequest]) -> RunResult:
    ok = sum(1 for r in rows if r.status == "ok")
    meta = RunMetadata(
        provider="mock", model="mock-fixtures-v1", prompt_version="v1",
        temperature=0.0, started_at="t0", finished_at="t1",
        duration_seconds=0.1, total_requests=len(rows), ok=ok, failed=len(rows) - ok,
    )
    return RunResult(metadata=meta, results=rows)


def analysis(**overrides) -> RequestAnalysis:
    base = dict(
        category="автоматизація", target_department="маркетинг", priority="medium",
        short_summary="Тестовий підсумок.", requested_actions=["Зробити щось"],
        needs_clarification=False, clarifying_questions=[],
        priority_rationale="Бо тест.", deadline_mentioned=None,
        confidence=0.8, is_multi_request=False, language="uk",
    )
    base.update(overrides)
    return RequestAnalysis.model_validate(base)


def ok_row(id_, **overrides) -> ProcessedRequest:
    return ProcessedRequest(
        id=id_, channel="Slack", timestamp="2026-06-08 09:00", raw_text="текст",
        status="ok", attempts=1, analysis=analysis(**overrides),
    )


class TestDigestText:
    def test_includes_run_metadata(self):
        text = build_digest_text(make_result([ok_row("REQ-001")]))
        assert "1 запитів" in text
        assert "mock/mock-fixtures-v1" in text
        assert "Успішно: 1" in text

    def test_high_priority_listed_with_summary(self):
        text = build_digest_text(make_result([ok_row("REQ-005", priority="high")]))
        assert "REQ-005" in text
        assert "Тестовий підсумок." in text
        assert "Високий пріоритет (1)" in text

    def test_no_high_priority_section_when_none(self):
        text = build_digest_text(make_result([ok_row("REQ-001", priority="low")]))
        assert "Високий пріоритет" not in text

    def test_needs_clarification_ids_listed(self):
        row = ok_row(
            "REQ-002", needs_clarification=True, requested_actions=[],
            clarifying_questions=["Що саме?"],
        )
        text = build_digest_text(make_result([row]))
        assert "Потребують уточнення (1): REQ-002" in text

    def test_low_confidence_section(self):
        text = build_digest_text(make_result([ok_row("REQ-011", confidence=0.3)]))
        assert "Низька впевненість моделі (1): REQ-011" in text

    def test_failed_requests_excluded_from_counts_but_counted_in_meta(self):
        failed = ProcessedRequest(
            id="REQ-999", channel="Slack", timestamp="t", raw_text="x",
            status="failed", attempts=2, analysis=None, error="llm_error: boom",
        )
        text = build_digest_text(make_result([ok_row("REQ-001"), failed]))
        assert "2 запитів" in text
        assert "помилок: 1" in text

    def test_truncates_long_digest(self):
        rows = [ok_row(f"REQ-{i:03d}", priority="high") for i in range(1, 400)]
        text = build_digest_text(make_result(rows))
        assert len(text) <= TELEGRAM_MAX_LEN
        assert text.endswith("(обрізано)")


class TestSheetsRow:
    def test_row_length_matches_columns(self):
        row = _row(ok_row("REQ-001"))
        assert len(row) == len(SHEET_COLUMNS)

    def test_failed_row_has_no_analysis_fields_but_keeps_error(self):
        failed = ProcessedRequest(
            id="REQ-999", channel="Slack", timestamp="t", raw_text="x",
            status="failed", attempts=2, analysis=None, error="llm_error: boom",
        )
        row = _row(failed)
        assert len(row) == len(SHEET_COLUMNS)
        assert row[SHEET_COLUMNS.index("error")] == "llm_error: boom"
        assert row[SHEET_COLUMNS.index("attempts")] == 2

    def test_lists_joined_with_separator(self):
        row = _row(ok_row("REQ-001", requested_actions=["Зробити A", "Зробити B"]))
        assert row[SHEET_COLUMNS.index("requested_actions")] == "Зробити A | Зробити B"


class TestSheetsMissingLibrary:
    def test_raises_integration_error_not_import_error(self):
        # У тестовому оточенні gspread не встановлено (requirements-optional.txt
        # окремо від базових залежностей) — це і є сценарій, який має ловити main.py.
        with pytest.raises(IntegrationError, match="gspread"):
            write_to_google_sheet(make_result([]), "fake-id", "fake-path.json")

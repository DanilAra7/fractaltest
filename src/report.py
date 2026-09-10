"""Агрегати по результатах. Жодних звернень до LLM — рахуємо по валідованих даних."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from .models import Category, ProcessedRequest, RunResult

# Поріг, нижче якого результат іде на ручне ревʼю навіть якщо він валідний.
LOW_CONFIDENCE_THRESHOLD = 0.6

PRIORITY_ORDER = ["high", "medium", "low"]


def _table(header: tuple[str, str], rows: list[tuple[str, int]], total: int) -> str:
    lines = [
        f"| {header[0]} | {header[1]} | % |",
        "|---|---:|---:|",
    ]
    for label, count in rows:
        share = f"{count / total * 100:.0f}%" if total else "—"
        lines.append(f"| {label} | {count} | {share} |")
    return "\n".join(lines)


def build_markdown(result: RunResult) -> str:
    meta = result.metadata
    ok = [r for r in result.results if r.status == "ok" and r.analysis]
    failed = [r for r in result.results if r.status == "failed"]
    total_ok = len(ok)

    by_category = Counter(r.analysis.category.value for r in ok)
    by_priority = Counter(r.analysis.priority for r in ok)
    by_department = Counter(
        r.analysis.target_department or "не визначено" for r in ok
    )

    needs_clarification = [r for r in ok if r.analysis.needs_clarification]
    low_confidence = [
        r for r in ok if r.analysis.confidence < LOW_CONFIDENCE_THRESHOLD
    ]
    multi = [r for r in ok if r.analysis.is_multi_request]
    repaired = [r for r in result.results if r.attempts > 1]
    with_deadline = [r for r in ok if r.analysis.deadline_mentioned]

    parts: list[str] = []
    parts.append("# Звіт по інбоксу запитів\n")
    parts.append(
        f"Провайдер: `{meta.provider}` · модель: `{meta.model}` · "
        f"промпт: `{meta.prompt_version}` · temperature: `{meta.temperature}`  \n"
        f"Прогін: {meta.started_at} → {meta.finished_at} "
        f"({meta.duration_seconds} с)  \n"
        f"Оброблено: **{meta.total_requests}** · успішно: **{meta.ok}** · "
        f"помилок: **{meta.failed}**\n"
    )

    parts.append("\n## За категоріями\n")
    category_rows = [
        (c.value, by_category.get(c.value, 0))
        for c in Category
        if by_category.get(c.value, 0)
    ]
    parts.append(_table(("Категорія", "Запитів"), category_rows, total_ok))

    parts.append("\n\n## За пріоритетом\n")
    priority_rows = [
        (p, by_priority.get(p, 0)) for p in PRIORITY_ORDER if by_priority.get(p, 0)
    ]
    parts.append(_table(("Пріоритет", "Запитів"), priority_rows, total_ok))

    parts.append("\n\n## За відділами\n")
    department_rows = sorted(
        by_department.items(), key=lambda kv: (-kv[1], kv[0])
    )
    parts.append(_table(("Відділ", "Запитів"), department_rows, total_ok))

    parts.append("\n\n## Потребують уточнення\n")
    if needs_clarification:
        parts.append(
            f"{len(needs_clarification)} з {total_ok} запитів не можна брати "
            "в роботу як є.\n"
        )
        for r in needs_clarification:
            parts.append(f"\n**{r.id}** ({r.channel}) — {r.analysis.short_summary}")
            parts.append(f"\n> «{r.raw_text}»\n")
            for question in r.analysis.clarifying_questions:
                parts.append(f"\n- {question}")
            parts.append("\n")
    else:
        parts.append("Немає — усі запити достатньо конкретні.\n")

    parts.append("\n## Високий пріоритет\n")
    high = [r for r in ok if r.analysis.priority == "high"]
    if high:
        for r in high:
            deadline = r.analysis.deadline_mentioned
            suffix = f" · дедлайн: **{deadline}**" if deadline else ""
            parts.append(
                f"- **{r.id}** ({r.analysis.target_department or 'відділ не визначено'})"
                f"{suffix} — {r.analysis.short_summary}\n"
                f"  <br>_чому high:_ {r.analysis.priority_rationale}\n"
            )
    else:
        parts.append("Немає.\n")

    parts.append("\n## Потребують уваги людини\n")
    if low_confidence:
        parts.append(
            f"\n**Низька впевненість моделі** (< {LOW_CONFIDENCE_THRESHOLD}) — "
            f"{len(low_confidence)}:\n"
        )
        for r in low_confidence:
            parts.append(
                f"- **{r.id}** — `{r.analysis.category.value}`, "
                f"confidence {r.analysis.confidence:.2f}\n"
            )
    if multi:
        parts.append(
            f"\n**Кілька задач в одному повідомленні** — {len(multi)}: "
            + ", ".join(f"**{r.id}**" for r in multi)
            + "\n"
        )
    if with_deadline:
        parts.append("\n**Зі згаданим дедлайном:**\n")
        for r in with_deadline:
            parts.append(f"- **{r.id}** — {r.analysis.deadline_mentioned}\n")
    if repaired:
        parts.append(
            f"\n**Вивід LLM довелось лагодити** (валідація не пройшла з першої "
            f"спроби) — {len(repaired)}: "
            + ", ".join(f"**{r.id}** ({r.attempts} спроби)" for r in repaired)
            + "\n"
        )
    if failed:
        parts.append(f"\n**Не вдалося обробити** — {len(failed)}:\n")
        for r in failed:
            parts.append(f"- **{r.id}** — `{r.error}`\n")
    if not (low_confidence or multi or with_deadline or repaired or failed):
        parts.append("Нічого — усе відпрацювало чисто.\n")

    return "".join(parts).rstrip() + "\n"


def write_csv(result: RunResult, path: Path) -> None:
    """Пласка таблиця для тих, кому зручніше в Google Sheets / Excel."""
    columns = [
        "id", "channel", "timestamp", "status", "category", "target_department",
        "priority", "priority_rationale", "short_summary", "requested_actions",
        "needs_clarification", "clarifying_questions", "deadline_mentioned",
        "confidence", "is_multi_request", "language", "attempts", "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for r in result.results:
            row = {
                "id": r.id, "channel": r.channel, "timestamp": r.timestamp,
                "status": r.status, "attempts": r.attempts, "error": r.error or "",
            }
            a = r.analysis
            if a:
                row.update(
                    category=a.category.value,
                    target_department=a.target_department or "",
                    priority=a.priority,
                    priority_rationale=a.priority_rationale,
                    short_summary=a.short_summary,
                    requested_actions=" | ".join(a.requested_actions),
                    needs_clarification=a.needs_clarification,
                    clarifying_questions=" | ".join(a.clarifying_questions),
                    deadline_mentioned=a.deadline_mentioned or "",
                    confidence=a.confidence,
                    is_multi_request=a.is_multi_request,
                    language=a.language,
                )
            writer.writerow(row)

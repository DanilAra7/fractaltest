"""Запис результату в Google Sheet через service account.

Використовує gspread + google-auth (requirements-optional.txt) — базова
установка проєкту цих бібліотек не тягне, щоб не роздувати залежності
заради опціонального плюсу.
"""

from __future__ import annotations

import re

from ..models import ProcessedRequest, RunResult
from .base import IntegrationError

# Люди природно копіюють з адресного рядка цілий URL, а не голий ID —
# так і сталося при першому реальному налаштуванні. Дешевше прийняти обидва
# формати, ніж ловити потім невиразну помилку "таблицю не знайдено".
_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_SHEET_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

SHEET_COLUMNS = [
    "id", "channel", "timestamp", "status",
    "category", "target_department", "priority", "priority_rationale",
    "short_summary", "requested_actions", "needs_clarification",
    "clarifying_questions", "deadline_mentioned", "confidence",
    "is_multi_request", "language", "attempts", "error",
]


def normalize_spreadsheet_id(value: str) -> str:
    """Приймає або голий ID таблиці, або будь-який URL, у якому він є.

    Google Sheets ID — це те, що між /d/ і наступним слешем. Усе інше
    (/edit, ?usp=sharing, #gid=0) відкидаємо.
    """
    value = (value or "").strip()
    if not value:
        raise IntegrationError("GOOGLE_SHEETS_SPREADSHEET_ID порожній")

    match = _SHEET_URL_RE.search(value)
    if match:
        return match.group(1)
    if _SHEET_ID_RE.match(value):
        return value
    raise IntegrationError(
        f"Не можу дістати ID таблиці з {value!r}. Очікую або сам ID, "
        "або посилання виду https://docs.google.com/spreadsheets/d/<ID>/edit"
    )


def _row(r: ProcessedRequest) -> list:
    a = r.analysis
    if a is None:
        empty = [""] * (len(SHEET_COLUMNS) - 6)
        return [r.id, r.channel, r.timestamp, r.status] + empty + [r.attempts, r.error or ""]
    return [
        r.id, r.channel, r.timestamp, r.status,
        a.category.value, a.target_department or "",
        a.priority, a.priority_rationale, a.short_summary,
        " | ".join(a.requested_actions), a.needs_clarification,
        " | ".join(a.clarifying_questions), a.deadline_mentioned or "",
        a.confidence, a.is_multi_request, a.language,
        r.attempts, r.error or "",
    ]


def write_to_google_sheet(
    result: RunResult,
    spreadsheet_id: str,
    credentials_path: str,
    worksheet_name: str = "Triage",
) -> str:
    """Перезаписує аркуш `worksheet_name` повним результатом прогону.

    Ідемпотентно: аркуш повністю очищається і заповнюється заново — щоб
    повторний прогін не дублював рядки. Повертає URL таблиці.
    Кидає IntegrationError на відсутню бібліотеку, биті креди чи мережу.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise IntegrationError(
            "Не встановлено gspread/google-auth. "
            "Постав: pip install -r requirements-optional.txt"
        ) from exc

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
    except FileNotFoundError as exc:
        raise IntegrationError(f"Файл креденшелів не знайдено: {credentials_path}") from exc
    except ValueError as exc:
        raise IntegrationError(f"Файл креденшелів пошкоджений або не той формат: {exc}") from exc

    # Нормалізація — поза try нижче: інакше її власне зрозуміле повідомлення
    # загорнулося б у "перевір права Editor", що збиває зі сліду.
    sheet_id = normalize_spreadsheet_id(spreadsheet_id)

    try:
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sheet_id)
    except Exception as exc:  # gspread ловить різні помилки авторизації одним класом
        raise IntegrationError(
            f"Не вдалося відкрити таблицю {sheet_id}: {exc}. "
            "Перевір, що service account додано як Editor до цієї таблиці."
        ) from exc

    try:
        try:
            ws = sheet.worksheet(worksheet_name)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(
                title=worksheet_name, rows=len(result.results) + 10, cols=len(SHEET_COLUMNS)
            )
        rows = [SHEET_COLUMNS] + [_row(r) for r in result.results]
        ws.update(values=rows, range_name="A1")
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(f"Не вдалося записати дані в аркуш: {exc}") from exc

    return sheet.url

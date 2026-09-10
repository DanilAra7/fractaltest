"""Схеми даних. Єдине джерело правди для промпту, валідації та звіту."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Category(str, Enum):
    AUTOMATION = "автоматизація"
    INTEGRATION = "інтеграція"
    REPORTING = "звіт/аналітика"
    BUG_SUPPORT = "баг/підтримка"
    QUESTION = "питання/консультація"
    OUT_OF_SCOPE = "поза скоупом"


Priority = Literal["low", "medium", "high"]
Language = Literal["uk", "ru", "en", "mixed"]

# Відділи, які реально існують у компанії. Все, що модель вигадає поза цим
# списком, нормалізується у None — краще порожнє поле, ніж вигаданий відділ.
KNOWN_DEPARTMENTS: dict[str, str] = {
    "маркетинг": "маркетинг",
    "marketing": "маркетинг",
    "продажі": "продажі",
    "продажи": "продажі",
    "sales": "продажі",
    "аналітика": "аналітика",
    "аналитика": "аналітика",
    "analytics": "аналітика",
    "pm": "PM",
    "проєктний менеджмент": "PM",
    "hr": "HR",
    "рекрутинг": "HR",
    "smm": "SMM",
    "контент": "контент",
    "content": "контент",
    "бухгалтерія": "бухгалтерія",
    "бухгалтерия": "бухгалтерія",
    "фінанси": "фінанси",
    "finance": "фінанси",
    "підтримка": "підтримка",
    "support": "підтримка",
}


class RequestAnalysis(BaseModel):
    """Те, що LLM має витягти з одного запиту."""

    model_config = {"extra": "forbid"}

    # --- обов'язкові поля з ТЗ ---
    category: Category
    target_department: str | None = Field(
        default=None, description="Відділ-замовник або null, якщо не зрозуміло"
    )
    priority: Priority
    short_summary: str = Field(min_length=5, max_length=300)
    requested_actions: list[str] = Field(default_factory=list)
    needs_clarification: bool

    # --- розширення схеми (обґрунтування — у README) ---
    clarifying_questions: list[str] = Field(
        default_factory=list,
        description="Що саме запитати в автора, якщо needs_clarification=true",
    )
    priority_rationale: str = Field(
        min_length=3,
        max_length=300,
        description="Чому саме такий пріоритет: тон, дедлайн, блокер",
    )
    deadline_mentioned: str | None = Field(
        default=None,
        description="Дедлайн дослівно з тексту, без парсингу в дату",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    is_multi_request: bool = False
    language: Language

    @field_validator("target_department", mode="after")
    @classmethod
    def _normalize_department(cls, v: str | None) -> str | None:
        if v is None:
            return None
        key = v.strip().lower()
        if not key or key in {"null", "none", "невідомо", "не зрозуміло", "-"}:
            return None
        return KNOWN_DEPARTMENTS.get(key)

    @field_validator("requested_actions", "clarifying_questions", mode="after")
    @classmethod
    def _clean_list(cls, v: list[str]) -> list[str]:
        return [item.strip() for item in v if item and item.strip()]

    @model_validator(mode="after")
    def _check_consistency(self) -> "RequestAnalysis":
        # Запит, готовий до роботи, зобов'язаний містити хоча б одну дію.
        # Виняток — "поза скоупом": там робити нічого й не треба.
        if (
            not self.needs_clarification
            and not self.requested_actions
            and self.category is not Category.OUT_OF_SCOPE
        ):
            raise ValueError(
                "needs_clarification=false, але requested_actions порожній: "
                "або вкажи дії, або постав needs_clarification=true"
            )
        if self.needs_clarification and not self.clarifying_questions:
            raise ValueError(
                "needs_clarification=true вимагає щонайменше одного "
                "питання в clarifying_questions"
            )
        return self


class InboxRequest(BaseModel):
    """Один рядок вхідного CSV."""

    id: str
    channel: str
    timestamp: str
    raw_text: str

    @field_validator("raw_text", mode="after")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("raw_text порожній")
        return v.strip()


class ProcessedRequest(BaseModel):
    """Результат по одному запиту разом з метаданими обробки."""

    id: str
    channel: str
    timestamp: str
    raw_text: str
    status: Literal["ok", "failed"]
    attempts: int
    analysis: RequestAnalysis | None = None
    error: str | None = None
    from_cache: bool = False


class RunMetadata(BaseModel):
    """Умови прогону — щоб output.json можна було відтворити й перевірити."""

    provider: str
    model: str
    prompt_version: str
    temperature: float
    started_at: str
    finished_at: str
    duration_seconds: float
    total_requests: int
    ok: int
    failed: int


class RunResult(BaseModel):
    metadata: RunMetadata
    results: list[ProcessedRequest]

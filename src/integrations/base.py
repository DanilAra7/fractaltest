"""Спільні речі для опціональних інтеграцій."""

from __future__ import annotations


class IntegrationError(RuntimeError):
    """Помилка Google Sheets / Telegram — не повинна валити основний прогін."""

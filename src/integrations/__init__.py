"""Опціональні інтеграції (Google Sheets, Telegram).

Обидві навмисно відв'язані від основного пайплайна: помилка тут — це
IntegrationError, яку main.py ловить окремо. Google Sheets чи Telegram
можуть впасти (мережа, чужі креди) вже після того, як output.json і
report.md успішно записані, і це не повинно перетворювати вдалий прогін
на провалений.
"""

from .base import IntegrationError

__all__ = ["IntegrationError"]

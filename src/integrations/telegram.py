"""Дайджест у Telegram через звичайний Bot API sendMessage.

Навмисно без бібліотек — це один POST-запит, stdlib urllib вистачає.
Додаткова залежність заради одного виклику API того не варта.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..models import Category, RunResult
from ..report import LOW_CONFIDENCE_THRESHOLD, PRIORITY_ORDER
from .base import IntegrationError

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_MAX_LEN = 4096
PRIORITY_LABEL = {"high": "high", "medium": "medium", "low": "low"}


def build_digest_text(result: RunResult) -> str:
    """Чистий текст дайджесту — без мережі, тому легко тестується."""
    meta = result.metadata
    ok = [r for r in result.results if r.status == "ok" and r.analysis]

    by_cat: dict[str, int] = {}
    by_pri: dict[str, int] = {}
    for r in ok:
        a = r.analysis
        by_cat[a.category.value] = by_cat.get(a.category.value, 0) + 1
        by_pri[a.priority] = by_pri.get(a.priority, 0) + 1

    needs_clar = [r for r in ok if r.analysis.needs_clarification]
    high = [r for r in ok if r.analysis.priority == "high"]
    low_conf = [r for r in ok if r.analysis.confidence < LOW_CONFIDENCE_THRESHOLD]

    lines: list[str] = []
    lines.append(f"Тріаж інбоксу — {meta.total_requests} запитів ({meta.provider}/{meta.model})")
    lines.append(f"Успішно: {meta.ok} · помилок: {meta.failed} · тривалість: {meta.duration_seconds}с")
    lines.append("")

    cat_line = " · ".join(f"{c.value} {by_cat[c.value]}" for c in Category if by_cat.get(c.value))
    if cat_line:
        lines.append("За категоріями: " + cat_line)
    pri_line = " · ".join(f"{PRIORITY_LABEL[p]} {by_pri[p]}" for p in PRIORITY_ORDER if by_pri.get(p))
    if pri_line:
        lines.append("Пріоритет: " + pri_line)

    if needs_clar:
        lines.append("")
        lines.append(f"Потребують уточнення ({len(needs_clar)}): " + ", ".join(r.id for r in needs_clar))

    if high:
        lines.append("")
        lines.append(f"Високий пріоритет ({len(high)}):")
        for r in high:
            lines.append(f"• {r.id} — {r.analysis.short_summary}")

    if low_conf:
        lines.append("")
        lines.append(f"Низька впевненість моделі ({len(low_conf)}): " + ", ".join(r.id for r in low_conf))

    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_LEN:
        # Telegram жорстко обрізає повідомлення довші за 4096 символів —
        # краще самим акуратно обрізати з приміткою, ніж дати впасти запиту.
        text = text[: TELEGRAM_MAX_LEN - 20].rstrip() + "\n… (обрізано)"
    return text


def send_telegram_digest(result: RunResult, bot_token: str, chat_id: str, timeout: float = 15.0) -> None:
    """Надсилає build_digest_text() як одне повідомлення в чат/канал.

    Кидає IntegrationError на будь-яку проблему з мережею чи API-ключем.
    """
    text = build_digest_text(result)
    url = TELEGRAM_API.format(token=bot_token)
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise IntegrationError(f"Telegram API повернув {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise IntegrationError(f"Не вдалося з'єднатися з Telegram API: {exc}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise IntegrationError(f"Telegram API: {exc}") from exc

    if not body.get("ok"):
        raise IntegrationError(f"Telegram API відповів помилкою: {body}")

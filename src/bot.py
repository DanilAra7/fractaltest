"""Telegram-бот поверх того самого пайплайну.

Навіщо: показати тріаж живій людині без CSV і без клонування репозиторію —
пишеш у чат довільний запит українською, у відповідь приходить та сама
структура, що лягає в output.json. Це не друга реалізація: бот викликає
`TriagePipeline.process_one()`, тобто той самий промпт, схему й ремонт
невалідного виводу.

Доступ закритий словом-паролем. Причина не стільки приватність, скільки
квота: бот ходить у Gemini, а безкоштовний тір — 5 запитів на хвилину, і
відкритий бот вигорів би за хвилину.

Транспорт — long polling через stdlib urllib, без зайвих залежностей, як і
в telegram.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from pathlib import Path

from .cache import ResponseCache
from .integrations.telegram import build_digest_text
from .llm import LLMError, get_provider
from .models import InboxRequest, ProcessedRequest, RequestAnalysis, RunResult
from .pipeline import TriagePipeline

log = logging.getLogger("bot")

API = "https://api.telegram.org/bot{token}/{method}"

# Скільки бот чекає на нові повідомлення в одному getUpdates.
POLL_TIMEOUT = 30
# Мінімальний інтервал між запитами одного чату. Захист квоти Gemini
# (безкоштовний тір — 5 запитів/хв), а не боротьба зі зловмисниками.
MIN_SECONDS_BETWEEN_REQUESTS = 6
# Довші повідомлення відсікаємо: це демо тріажу, а не спосіб згодувати
# моделі книгу за чужий рахунок.
MAX_INPUT_CHARS = 2000
TELEGRAM_MAX_LEN = 4096

GREETING = (
    "Це демо тріажу вхідних запитів AI-юніту.\n\n"
    "Надішли будь-який запит у вільній формі — так, як його написали б у Slack "
    "чи в пошті, — і у відповідь прийде розібрана структура: категорія, відділ, "
    "пріоритет, конкретні дії, чи треба уточнювати.\n\n"
    "Щоб почати, надішли слово-пароль."
)

WRONG_PASSWORD = "Не те слово. Спробуй ще раз."
UNLOCKED_INTRO = (
    "Готово.\n\n"
    "Спочатку — те, заради чого сервіс і робився: звіт по тестовому інбоксу "
    "(input_requests.csv, 18 запитів). Рівно ті самі агрегати, що лягають у "
    "output.json і report.md."
)

READY_FOR_INPUT = (
    "А тепер можна погратись: надішли будь-який свій запит у вільній формі — "
    "розберу так само, тим самим промптом і тією самою схемою.\n\n"
    "Наприклад: «Привіт! Можна автоматизувати щотижневий звіт по Google Ads? "
    "Зараз руками вивантажую CSV, займає годину».\n\n"
    "/report — показати звіт ще раз."
)

REPORT_MISSING = (
    "Не знайшов файл зі звітом. Спочатку треба прогнати сервіс: "
    "python -m src.main --provider gemini"
)

# Куди бот дивиться за звітом. Спершу реальний прогін моделі, потім мок —
# щоб демо показувало Gemini, але не ламалось у чистому клоні без ключа.
REPORT_CANDIDATES = ("out/gemini/output.json", "out/output.json")


class BotState:
    """Хто вже ввів пароль і коли востаннє щось просив.

    У памʼяті, без бази: після рестарту пароль треба ввести знову. Для демо
    цього достатньо, а на Railway диск усе одно ефемерний.
    """

    def __init__(self) -> None:
        self.authorized: set[int] = set()
        self.last_request_at: dict[int, float] = {}

    def is_authorized(self, chat_id: int) -> bool:
        return chat_id in self.authorized

    def authorize(self, chat_id: int) -> None:
        self.authorized.add(chat_id)

    def seconds_until_allowed(self, chat_id: int, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        last = self.last_request_at.get(chat_id)
        if last is None:
            return 0.0
        return max(0.0, MIN_SECONDS_BETWEEN_REQUESTS - (now - last))

    def mark_request(self, chat_id: int, now: float | None = None) -> None:
        self.last_request_at[chat_id] = time.monotonic() if now is None else now


def format_analysis(analysis: RequestAnalysis) -> str:
    """Той самий набір полів, що і в output.json, але читабельно для чату."""
    lines = [f"Категорія: {analysis.category.value}"]
    lines.append(f"Пріоритет: {analysis.priority} — {analysis.priority_rationale}")
    lines.append(f"Відділ: {analysis.target_department or 'не визначено'}")
    lines.append("")
    lines.append(f"Суть: {analysis.short_summary}")

    if analysis.requested_actions:
        lines.append("")
        lines.append("Що просять зробити:")
        lines.extend(f"• {action}" for action in analysis.requested_actions)

    if analysis.needs_clarification:
        lines.append("")
        lines.append("Запит надто розмитий, щоб брати як є. Варто запитати:")
        lines.extend(f"• {q}" for q in analysis.clarifying_questions)

    extras = []
    if analysis.deadline_mentioned:
        extras.append(f"дедлайн: {analysis.deadline_mentioned}")
    if analysis.is_multi_request:
        extras.append("кілька задач в одному повідомленні")
    extras.append(f"впевненість моделі: {analysis.confidence:.0%}")

    lines.append("")
    lines.append(" · ".join(extras))
    return "\n".join(lines)


def format_failure(result: ProcessedRequest) -> str:
    """Навіть поламаний прогін показуємо чесно, а не мовчазним 'щось пішло не так'."""
    error = result.error or "невідома помилка"
    if "429" in error or "RESOURCE_EXHAUSTED" in error:
        return (
            "Уперся в ліміт безкоштовного тіру Gemini (5 запитів на хвилину). "
            "Спробуй ще раз за хвилину."
        )
    return f"Не вдалося розібрати запит: {error}"


def load_report_digest(root: Path | None = None) -> str | None:
    """Дайджест по вже порахованому прогону — без звернень до моделі.

    Звіт не перераховується на льоту навмисно: 18 запитів на безкоштовному
    тірі Gemini — це кілька хвилин через ліміт 5/хв, а в чаті людина стільки
    не чекатиме. Беремо готовий output.json і форматуємо тією самою
    build_digest_text(), що й дайджест для --telegram-digest.
    """
    root = root or Path(__file__).resolve().parent.parent
    override = os.getenv("TRIAGE_REPORT_FILE")
    candidates = [Path(override)] if override else [root / c for c in REPORT_CANDIDATES]

    for path in candidates:
        if not path.exists():
            continue
        try:
            result = RunResult.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("не зміг прочитати звіт %s: %s", path, exc)
            continue
        return build_digest_text(result)
    return None


async def build_reply(state: BotState, chat_id: int, text: str, analyze) -> list[str]:
    """Уся логіка відповіді. Мережі тут немає — тому це можна тестувати.

    `analyze` — корутина, що приймає текст і повертає ProcessedRequest.
    Повертає список повідомлень (порожній — відповідати не треба): після
    введення пароля їх три — вступ, звіт по датасету, запрошення до вводу.
    """
    text = (text or "").strip()
    if not text:
        return []

    if text in {"/start", "/help"}:
        return [GREETING] if not state.is_authorized(chat_id) else [READY_FOR_INPUT]

    if not state.is_authorized(chat_id):
        expected = os.getenv("TELEGRAM_ACCESS_WORD", "")
        # Порівнюємо байти, а не рядки: compare_digest не приймає non-ASCII,
        # а сюди прилітає українська — на ній воно падало з TypeError.
        if expected and secrets.compare_digest(
            text.strip().lower().encode("utf-8"),
            expected.strip().lower().encode("utf-8"),
        ):
            state.authorize(chat_id)
            return [UNLOCKED_INTRO, load_report_digest() or REPORT_MISSING, READY_FOR_INPUT]
        return [WRONG_PASSWORD]

    if text == "/report":
        return [load_report_digest() or REPORT_MISSING]

    if len(text) > MAX_INPUT_CHARS:
        return [f"Задовгий текст ({len(text)} символів). Максимум {MAX_INPUT_CHARS}."]

    wait = state.seconds_until_allowed(chat_id)
    if wait > 0:
        return [f"Занадто часто. Спробуй за {wait:.0f} с — бережу квоту Gemini."]

    state.mark_request(chat_id)
    result = await analyze(text)
    if result.status != "ok" or result.analysis is None:
        return [format_failure(result)]
    return [format_analysis(result.analysis)]


def _api_call(token: str, method: str, payload: dict, timeout: float) -> dict:
    url = API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def api_call(token: str, method: str, payload: dict, timeout: float = 60.0) -> dict:
    return await asyncio.to_thread(_api_call, token, method, payload, timeout)


async def send_message(token: str, chat_id: int, text: str) -> None:
    if len(text) > TELEGRAM_MAX_LEN:
        text = text[: TELEGRAM_MAX_LEN - 20].rstrip() + "\n… (обрізано)"
    try:
        await api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        log.warning("не вдалося надіслати відповідь у чат %s: %s", chat_id, exc)


async def run_bot() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("[error] TELEGRAM_BOT_TOKEN не заданий")
        return 2
    if not os.getenv("TELEGRAM_ACCESS_WORD"):
        print("[error] TELEGRAM_ACCESS_WORD не заданий — бот був би відкритий для всіх")
        return 2

    try:
        provider = get_provider("gemini", os.getenv("GEMINI_MODEL"), 0.0)
    except LLMError as exc:
        print(f"[error] {exc}")
        return 2

    pipeline = TriagePipeline(
        provider=provider,
        cache=ResponseCache(_cache_dir(), enabled=True),
        concurrency=2,
        max_repair_attempts=1,
    )

    async def analyze(text: str) -> ProcessedRequest:
        request = InboxRequest(
            id="telegram",
            channel="Telegram",
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            raw_text=text,
        )
        return await pipeline.process_one(request)

    state = BotState()
    me = await api_call(token, "getMe", {})
    print(f"Бот запущений: @{me['result'].get('username')} · модель {provider.model}")

    offset = 0
    while True:
        try:
            updates = await api_call(
                token,
                "getUpdates",
                {"offset": offset, "timeout": POLL_TIMEOUT},
                timeout=POLL_TIMEOUT + 15,
            )
        except Exception as exc:  # мережа моргнула — не привід падати
            log.warning("getUpdates не вдався: %s", exc)
            await asyncio.sleep(3)
            continue

        for update in updates.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            text = message.get("text")
            if chat_id is None or text is None:
                continue
            try:
                replies = await build_reply(state, chat_id, text, analyze)
            except Exception as exc:
                log.exception("помилка обробки повідомлення")
                replies = [f"Внутрішня помилка: {exc}"]
            for reply in replies:
                await send_message(token, chat_id, reply)


def _cache_dir():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "out" / ".cache"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        return asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\nЗупинено")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

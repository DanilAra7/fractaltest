"""CLI: python -m src.main --provider mock"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .cache import ResponseCache
from .integrations import IntegrationError
from .llm import LLMError, get_provider
from .models import RunResult
from .pipeline import TriagePipeline, read_inbox
from .report import build_markdown, write_csv

ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="triage", description="Класифікація та структурування інбоксу запитів."
    )
    parser.add_argument("--input", type=Path, default=ROOT / "data/input_requests.csv")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "out")
    parser.add_argument(
        "--provider", choices=["mock", "gemini"], default="mock",
        help="mock працює без ключа на фікстурах; gemini — реальні виклики",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--concurrency", type=int, default=5,
        help="Скільки запитів обробляти паралельно (обережно з rate limits)",
    )
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--repair-attempts", type=int, default=1,
        help="Скільки разів перепитувати модель після помилки валідації",
    )
    parser.add_argument(
        "--write-sheet", action="store_true",
        help="Записати результат у Google Sheet (потрібні GOOGLE_SHEETS_*, див. .env.example)",
    )
    parser.add_argument(
        "--telegram-digest", action="store_true",
        help="Надіслати дайджест у Telegram (потрібні TELEGRAM_*, див. .env.example)",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    if not args.input.exists():
        print(f"[error] немає вхідного файлу: {args.input}", file=sys.stderr)
        return 2

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    requests = read_inbox(args.input)
    if not requests:
        print("[error] у вхідному файлі немає валідних запитів", file=sys.stderr)
        return 2

    try:
        provider = get_provider(args.provider, args.model, args.temperature)
    except LLMError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    print(
        f"Обробка {len(requests)} запитів · провайдер={provider.name} "
        f"модель={provider.model} паралельно={args.concurrency}"
    )
    if provider.name == "mock":
        print(
            "[warn] МОК-ПРОВАЙДЕР: відповіді беруться з фікстур, реальних "
            "викликів LLM немає. Для справжнього прогону: --provider gemini"
        )

    pipeline = TriagePipeline(
        provider=provider,
        cache=ResponseCache(args.out_dir / ".cache", enabled=not args.no_cache),
        concurrency=args.concurrency,
        max_repair_attempts=args.repair_attempts,
    )
    result = await pipeline.run(requests)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "output.json"
    md_path = args.out_dir / "report.md"
    csv_path = args.out_dir / "report.csv"

    json_path.write_text(
        result.model_dump_json(indent=2, exclude_none=False), encoding="utf-8"
    )
    md_path.write_text(build_markdown(result), encoding="utf-8")
    write_csv(result, csv_path)

    meta = result.metadata
    cached = sum(1 for r in result.results if r.from_cache)
    repaired = sum(1 for r in result.results if r.attempts > 1)
    print(
        f"Готово за {meta.duration_seconds} с: ok={meta.ok} failed={meta.failed} "
        f"з кешу={cached} полагоджено={repaired}"
    )
    print(f"  {json_path}\n  {md_path}\n  {csv_path}")

    # Опціональні плюси. Обидва йдуть уже після того, як output.json і
    # report.md успішно записані на диск — падіння тут не повинно
    # перетворювати вдалий прогін на провалений, тому ловимо окремо.
    if args.write_sheet:
        _write_sheet(result)
    if args.telegram_digest:
        _send_telegram_digest(result)

    return 1 if meta.failed else 0


def _write_sheet(result: RunResult) -> None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
    credentials_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE")
    if not spreadsheet_id or not credentials_path:
        print(
            "[warn] --write-sheet задано, але GOOGLE_SHEETS_SPREADSHEET_ID / "
            "GOOGLE_SHEETS_CREDENTIALS_FILE не задані — пропускаю"
        )
        return
    try:
        from .integrations.sheets import write_to_google_sheet

        url = write_to_google_sheet(result, spreadsheet_id, credentials_path)
        print(f"  Google Sheet оновлено: {url}")
    except IntegrationError as exc:
        print(f"[warn] Google Sheets: {exc}")


def _send_telegram_digest(result: RunResult) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(
            "[warn] --telegram-digest задано, але TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID не задані — пропускаю"
        )
        return
    try:
        from .integrations.telegram import send_telegram_digest

        send_telegram_digest(result, token, chat_id)
        print("  Дайджест надіслано в Telegram")
    except IntegrationError as exc:
        print(f"[warn] Telegram: {exc}")


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

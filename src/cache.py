"""Кеш відповідей на диску.

Ключ = текст запиту + версія промпту + модель. Перезапуски під час розробки
безкоштовні, а правка промпту інвалідує кеш сама, без ручного чищення.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class ResponseCache:
    def __init__(self, directory: Path, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled
        if enabled:
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(raw_text: str, prompt_version: str, model: str) -> str:
        payload = f"{prompt_version}|{model}|{raw_text}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:32]

    def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        path = self.directory / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Побитий файл кешу — не причина валити прогін.
            return None

    def set(self, key: str, value: dict) -> None:
        if not self.enabled:
            return
        path = self.directory / f"{key}.json"
        try:
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

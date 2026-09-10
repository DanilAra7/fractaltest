"""Крихітний HTTP-сервер поруч із ботом.

Потрібен не боту, а хостингу: Hugging Face Spaces (як і більшість платформ)
вважає контейнер живим лише якщо той слухає порт. Бот на long polling жодного
порту не слухає, тому без цього Space одразу падав би в 'unhealthy'.

Заразом сторінка Space стає корисною: видно, що бот живий, яка модель, скільки
працює і куди писати. Слово-пароль тут свідомо не показується — сторінка
публічна.
"""

from __future__ import annotations

import html
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("health")

DEFAULT_PORT = 7860

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto;
         padding: 0 1.5rem; line-height: 1.6; color: #1b1f1a; background: #f3f4ee; }}
  h1 {{ font-size: 1.5rem; margin-bottom: .25rem; }}
  .muted {{ color: #5b6058; }}
  dl {{ display: grid; grid-template-columns: auto 1fr; gap: .4rem 1rem; margin: 1.5rem 0; }}
  dt {{ color: #5b6058; }}
  dd {{ margin: 0; font-family: ui-monospace, monospace; }}
  .ok {{ color: #2f6a5c; font-weight: 600; }}
  a {{ color: #2f6a5c; }}
</style>
<h1>{title}</h1>
<p class="muted">Демо тріажу вхідних запитів: надсилаєш запит у вільній формі —
отримуєш структуровану картку (категорія, відділ, пріоритет, дії).</p>
<dl>
  <dt>Статус</dt><dd class="ok">працює</dd>
  <dt>Бот</dt><dd>{bot}</dd>
  <dt>Модель</dt><dd>{model}</dd>
  <dt>Аптайм</dt><dd>{uptime}</dd>
</dl>
<p class="muted">Щоб скористатись, потрібне слово-пароль — воно не публікується тут.
Код і пояснення: <a href="{repo}">{repo}</a></p>
"""


class HealthServer:
    def __init__(self, bot_username: str, model: str, repo_url: str) -> None:
        self.bot_username = bot_username
        self.model = model
        self.repo_url = repo_url
        self.started_at = time.monotonic()

    def _uptime(self) -> str:
        seconds = int(time.monotonic() - self.started_at)
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours} год {minutes} хв"
        if minutes:
            return f"{minutes} хв {secs} с"
        return f"{secs} с"

    def render(self) -> str:
        bot = f"@{self.bot_username}" if self.bot_username else "невідомо"
        return PAGE.format(
            title="Netpeak · тріаж інбоксу",
            bot=html.escape(bot),
            model=html.escape(self.model or "—"),
            uptime=self._uptime(),
            repo=html.escape(self.repo_url),
        )

    def start(self, port: int | None = None) -> bool:
        """Піднімає сервер у фоновому потоці. False — якщо не вдалося.

        Невдача не критична: локально порт може бути зайнятий, і це не привід
        не запускати бота.
        """
        port = port if port is not None else int(os.getenv("PORT", DEFAULT_PORT))
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (ім'я диктує stdlib)
                body = server.render().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass  # не засмічуємо лог бота запитами хелсчекера

        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        except OSError as exc:
            log.warning("не вдалося зайняти порт %s: %s", port, exc)
            return False

        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        log.info("health-сторінка на порту %s", port)
        return True

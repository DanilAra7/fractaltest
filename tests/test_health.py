"""Тести health-сторінки. Вона існує заради хостингу, тому найважливіше —
що порт справді слухається і що на публічну сторінку не тече пароль."""

from __future__ import annotations

import socket
import urllib.request

import pytest

from src.health import HealthServer

REPO = "https://github.com/DanilAra7/fractaltest"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestRender:
    def test_shows_bot_and_model(self):
        page = HealthServer("mybot", "gemini-3.6-flash", REPO).render()
        assert "@mybot" in page
        assert "gemini-3.6-flash" in page

    def test_never_leaks_access_word(self, monkeypatch):
        # Сторінка публічна: пароль на ній не має зʼявитись за жодних обставин.
        monkeypatch.setenv("TELEGRAM_ACCESS_WORD", "супер-таємне-слово")
        page = HealthServer("mybot", "model", REPO).render()
        assert "супер-таємне-слово" not in page

    def test_missing_bot_name_does_not_break_page(self):
        page = HealthServer("", "", REPO).render()
        assert "невідомо" in page

    def test_html_is_escaped(self):
        page = HealthServer("<script>alert(1)</script>", "m", REPO).render()
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_uptime_rendered(self):
        assert "с" in HealthServer("b", "m", REPO).render()


class TestServer:
    def test_serves_over_http(self):
        port = free_port()
        server = HealthServer("mybot", "gemini-3.6-flash", REPO)
        assert server.start(port) is True

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
        assert "@mybot" in body

    def test_busy_port_does_not_raise(self):
        port = free_port()
        first = HealthServer("a", "m", REPO)
        assert first.start(port) is True
        # Другий сервер на тому ж порту має тихо здатись, а не впасти:
        # бот повинен працювати навіть без сторінки.
        assert HealthServer("b", "m", REPO).start(port) is False

"""Подключение аккаунта ВК.

Тест с настоящим Chrome (без окна) по умолчанию пропущен:

    VKSTREAM_BROWSER_TESTS=1 pytest tests/test_login.py
"""

from __future__ import annotations

import os

import pytest
from aiohttp import web

from vkstream import login as login_module
from vkstream.vk import cookies as cookies_module
from vkstream.vk.client import VKClient
from vkstream.vk.cookies import CookieStore

from .conftest import FakeVK, write_session

needs_chrome = pytest.mark.skipif(
    not os.getenv("VKSTREAM_BROWSER_TESTS"), reason="нужен Chrome; включается VKSTREAM_BROWSER_TESTS=1"
)

# Страница «входа»: через секунду после открытия ставит cookie сессии, как это делает vk.ru
LOGIN_PAGE = """<html><body>Вход…<script>
setTimeout(() => { document.cookie = "remixsid=good; path=/"; }, 1000);
</script></body></html>"""


def fake_browser(value: str = "good"):
    """Подмена окна браузера: «пользователь вошёл», сессия получена."""

    async def browser_login(_cfg, vk: VKClient, _headless: bool = False) -> None:
        vk.adopt(CookieStore.from_header(f"remixsid={value}", "127.0.0.1"), "UA/1.0")
        await vk.refresh_token(force=True)

    return browser_login


async def authorized_name(cfg) -> tuple[str, str]:
    vk = VKClient(cfg)
    try:
        await vk.authorize()
        return vk.me_name, vk.user_agent
    finally:
        await vk.close()


@needs_chrome
async def test_browser_login_reads_session_from_real_chrome(cfg, fake_vk: FakeVK, monkeypatch, capsys):
    async def im_page(_request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_PAGE, content_type="text/html")

    fake_vk.pages["/im"] = im_page
    cfg.session_path.unlink()
    # поддельный ВК живёт на 127.0.0.1 — на время теста считаем его «доменом ВК»
    monkeypatch.setattr(cookies_module, "VK_DOMAINS", ("127.0.0.1",))
    monkeypatch.setattr(login_module, "POLL_INTERVAL", 0.3)

    await login_module.login(cfg, headless=True)

    assert "вы вошли как Иван Петров" in capsys.readouterr().out
    name, user_agent = await authorized_name(cfg)
    assert name == "Иван Петров"
    assert "Chrome/" in user_agent and "Headless" not in user_agent


@needs_chrome
async def test_browser_login_stops_when_window_is_closed(cfg, fake_vk: FakeVK, monkeypatch):
    async def closing_page(_request: web.Request) -> web.Response:
        return web.Response(text="<script>setTimeout(() => window.close(), 300)</script>", content_type="text/html")

    fake_vk.pages["/im"] = closing_page
    monkeypatch.setattr(login_module, "POLL_INTERVAL", 0.5)
    with pytest.raises(SystemExit, match="закрыто до завершения входа"):
        await login_module.login(cfg, headless=True)


async def test_login_saves_session_usable_by_client(cfg, fake_vk: FakeVK, monkeypatch, capsys):
    cfg.session_path.unlink()
    monkeypatch.setattr(login_module, "_browser_login", fake_browser())
    await login_module.login(cfg)

    assert "вы вошли как Иван Петров" in capsys.readouterr().out
    assert oct(cfg.session_path.stat().st_mode & 0o777) == "0o600"
    assert await authorized_name(cfg) == ("Иван Петров", "UA/1.0")


async def test_relogin_replaces_dead_saved_session(cfg, fake_vk: FakeVK, monkeypatch):
    write_session(cfg, "протухла")  # прежняя сессия на диске больше не действует
    monkeypatch.setattr(login_module, "_browser_login", fake_browser())
    await login_module.login(cfg)
    assert "good" in cfg.session_path.read_text(encoding="utf-8")
    assert (await authorized_name(cfg))[0] == "Иван Петров"


async def test_login_rejects_session_vk_does_not_accept(cfg, fake_vk: FakeVK, monkeypatch):
    cfg.session_path.unlink()
    monkeypatch.setattr(login_module, "_browser_login", fake_browser("чужая"))
    with pytest.raises(SystemExit, match="ВК не принял сессию"):
        await login_module.login(cfg)
    assert not cfg.session_path.exists()  # негодную сессию на диск не кладём


async def test_cookie_login_checks_and_saves_session(cfg, fake_vk: FakeVK, monkeypatch, capsys):
    cfg.session_path.unlink()
    monkeypatch.setattr(login_module.getpass, "getpass", lambda _prompt: "remixlang=0; other=1")
    with pytest.raises(SystemExit, match="remixsid"):
        await login_module.login(cfg, from_cookie=True)

    # cookie из DevTools привязываются к vk.ru; поддельный ВК живёт на 127.0.0.1
    real = CookieStore.from_header
    monkeypatch.setattr(login_module.CookieStore, "from_header", lambda header: real(header, "127.0.0.1"))
    monkeypatch.setattr(login_module.getpass, "getpass", lambda _prompt: "Cookie: remixsid=good; p=xyz")
    await login_module.login(cfg, from_cookie=True)
    assert "вы вошли как Иван Петров" in capsys.readouterr().out
    assert (await authorized_name(cfg))[0] == "Иван Петров"


def test_browser_is_found_or_overridden(monkeypatch, tmp_path):
    fake = tmp_path / "my-chrome"
    fake.write_text("")
    monkeypatch.setenv("VKSTREAM_BROWSER", str(fake))
    assert login_module.find_browser() == str(fake)

    monkeypatch.delenv("VKSTREAM_BROWSER")
    monkeypatch.setattr(login_module, "MAC_BROWSERS", (str(tmp_path / "нет"), str(fake)))
    assert login_module.find_browser() == str(fake)

    monkeypatch.setattr(login_module, "MAC_BROWSERS", ())
    monkeypatch.setattr(login_module, "LINUX_BROWSERS", ())
    monkeypatch.setattr(login_module, "WINDOWS_BROWSERS", ())
    assert login_module.find_browser() is None


async def test_no_browser_points_to_cookie_login(cfg, monkeypatch):
    monkeypatch.setattr(login_module, "find_browser", lambda: None)
    with pytest.raises(SystemExit, match="--cookie"):
        await login_module.login(cfg)

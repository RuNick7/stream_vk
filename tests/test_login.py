"""Подключение аккаунта ВК.

Тест с настоящим Chrome (без окна) по умолчанию пропущен:

    VKSTREAM_BROWSER_TESTS=1 pytest tests/test_login.py
"""

from __future__ import annotations

import os

import pytest
from aiohttp import web

from vkstream import login as login_module
from vkstream.vk.client import VKClient

from .conftest import FakeVK

needs_chrome = pytest.mark.skipif(
    not os.getenv("VKSTREAM_BROWSER_TESTS"), reason="нужен Chrome; включается VKSTREAM_BROWSER_TESTS=1"
)

# Страница «входа»: через секунду после открытия ставит cookie сессии, как это делает vk.ru
LOGIN_PAGE = """<html><body>Вход…<script>
setTimeout(() => { document.cookie = "remixsid=good; path=/"; }, 1000);
</script></body></html>"""


@needs_chrome
async def test_browser_login_waits_for_session_and_returns_its_cookies(cfg, fake_vk: FakeVK):
    async def im_page(_request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_PAGE, content_type="text/html")

    fake_vk.pages["/im"] = im_page
    cookies, user_agent = await login_module._browser_login(cfg, headless=True)

    assert {"remixsid": "good"}.items() <= {c["name"]: c["value"] for c in cookies}.items()
    assert "Mozilla" in user_agent
    assert fake_vk.logins >= 1  # готовность сессии проверена запросом web_token


async def test_login_saves_session_usable_by_client(cfg, fake_vk: FakeVK, monkeypatch, capsys):
    cfg.session_path.unlink()

    async def fake_browser(_cfg):
        return [{"name": "remixsid", "value": "good", "domain": ".vk.ru", "path": "/", "expires": -1}], "UA/1.0"

    monkeypatch.setattr(login_module, "_browser_login", fake_browser)
    # cookie домена vk.ru не уйдут на 127.0.0.1 — для проверки считаем тестовый хост «доменом ВК»
    monkeypatch.setattr(
        login_module.CookieStore,
        "from_browser",
        classmethod(lambda cls, items: cls.from_header("; ".join(f"{c['name']}={c['value']}" for c in items), "127.0.0.1")),
    )
    await login_module.login(cfg)

    assert "вы вошли как Иван Петров" in capsys.readouterr().out
    assert oct(cfg.session_path.stat().st_mode & 0o777) == "0o600"
    vk = VKClient(cfg)
    try:
        await vk.authorize()
        assert (vk.me_name, vk.user_agent) == ("Иван Петров", "UA/1.0")
    finally:
        await vk.close()


async def test_login_rejects_session_vk_does_not_accept(cfg, fake_vk: FakeVK, monkeypatch):
    cfg.session_path.unlink()
    fake_vk.session_ok = False

    async def fake_browser(_cfg):
        return [{"name": "remixsid", "value": "good", "domain": ".vk.ru", "path": "/", "expires": -1}], "UA/1.0"

    monkeypatch.setattr(login_module, "_browser_login", fake_browser)
    monkeypatch.setattr(
        login_module.CookieStore,
        "from_browser",
        classmethod(lambda cls, items: cls.from_header("remixsid=good", "127.0.0.1")),
    )
    with pytest.raises(SystemExit, match="ВК не принял сессию"):
        await login_module.login(cfg)
    assert not cfg.session_path.exists()  # негодную сессию на диск не кладём


def test_cookie_login_requires_session_cookie(monkeypatch):
    monkeypatch.setattr(login_module.getpass, "getpass", lambda _prompt: "remixlang=0; other=1")
    with pytest.raises(SystemExit, match="remixsid"):
        login_module._ask_cookies()
    monkeypatch.setattr(login_module.getpass, "getpass", lambda _prompt: "Cookie: remixsid=abc; p=xyz")
    assert login_module._ask_cookies().header_for("https://login.vk.ru/") == "remixsid=abc; p=xyz"

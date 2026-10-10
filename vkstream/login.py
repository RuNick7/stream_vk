"""Подключение аккаунта ВК: вход в окне браузера или импорт cookie.

Пароль программа не видит и не хранит: вы входите на vk.ru сами, а на диск
сохраняются только cookie сессии (data/vk_session.json, права 0600).

Браузер запускается как обычный Chrome, без флагов автоматизации, с пустым
временным профилем. Программа только читает из него cookie через DevTools.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import socket
import subprocess
import tempfile

import aiohttp

from .config import Config
from .vk.client import VKAuthError, VKClient
from .vk.cookies import CookieStore

LOGIN_TIMEOUT = 600  # сколько ждать, пока вы войдёте
STARTUP_TIMEOUT = 30  # сколько ждать запуска браузера
POLL_INTERVAL = 2.0

MAC_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
LINUX_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
)
WINDOWS_BROWSERS = (
    ("Google", "Chrome", "Application", "chrome.exe"),
    ("Microsoft", "Edge", "Application", "msedge.exe"),
)


async def login(cfg: Config, from_cookie: bool = False, headless: bool = False) -> None:
    vk = VKClient(cfg)
    try:
        if from_cookie:
            vk.adopt(_ask_cookies())
            await vk.refresh_token(force=True)
        else:
            await _browser_login(cfg, vk, headless)
        # Проверяем сессию тем же путём, каким потом будет ходить бот
        users = await vk.call("users.get")
    except VKAuthError as e:
        raise SystemExit(f"ВК не принял сессию: {e.message}. Попробуйте войти ещё раз.") from None
    finally:
        await vk.close()
    vk.save_session()
    name = f"{users[0].get('first_name', '')} {users[0].get('last_name', '')}".strip() if users else "?"
    print(f"Готово: вы вошли как {name}. Сессия сохранена в {cfg.session_path}")


def _ask_cookies() -> CookieStore:
    print(
        "Откройте vk.ru в своём браузере (вы должны быть в аккаунте), затем DevTools -> Network,\n"
        "обновите страницу, найдите запрос «?act=web_token» и скопируйте значение заголовка\n"
        "запроса Cookie целиком."
    )
    cookies = CookieStore.from_header(getpass.getpass("Cookie (ввод скрыт): "))
    if not cookies.get("remixsid"):
        raise SystemExit("В строке нет cookie remixsid — скопируйте заголовок Cookie целиком.")
    return cookies


def find_browser() -> str | None:
    """Путь к Chrome или другому браузеру на Chromium; VKSTREAM_BROWSER задаёт его явно."""
    override = os.getenv("VKSTREAM_BROWSER")
    if override:
        return override
    candidates = list(MAC_BROWSERS)
    candidates += [path for path in map(shutil.which, LINUX_BROWSERS) if path]
    for root in filter(None, map(os.getenv, ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"))):
        candidates += [os.path.join(root, *parts) for parts in WINDOWS_BROWSERS]
    return next((path for path in candidates if os.path.exists(path)), None)


async def _browser_login(cfg: Config, vk: VKClient, headless: bool = False) -> None:
    """Открыть vk.ru в окне браузера и дождаться, пока вход даст рабочую сессию."""
    binary = find_browser()
    if binary is None:
        raise SystemExit(
            "Не нашёл Google Chrome. Установите его, укажите путь к браузеру в переменной\n"
            "VKSTREAM_BROWSER или войдите через cookie: python -m vkstream login --cookie"
        )
    # Профиль временный: после входа от него нужны только cookie
    profile = tempfile.mkdtemp(prefix="vkstream-login-")
    port = _free_port()
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args.append("--headless=new")
    proc = subprocess.Popen([*args, cfg.vk_origin + "/im"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    devtools = f"http://127.0.0.1:{port}"
    # Свой HTTP-клиент для DevTools: у клиента ВК может быть настроен прокси
    local = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
    try:
        info = await _wait_for_browser(local, devtools, proc)
        user_agent = str(info.get("User-Agent") or "").replace("HeadlessChrome", "Chrome")
        print("Войдите в свой аккаунт ВК в открывшемся окне — оно закроется само.")
        print("Если страница долго не открывается, обновите её: программа подождёт.")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOGIN_TIMEOUT
        async with local.ws_connect(info["webSocketDebuggerUrl"], max_msg_size=0) as ws:
            while loop.time() < deadline:
                await asyncio.sleep(POLL_INTERVAL)
                if proc.poll() is not None or not await _has_open_tabs(local, devtools):
                    raise SystemExit("Окно браузера закрыто до завершения входа.")
                found = await _devtools(ws, "Storage.getCookies")
                cookies = CookieStore.from_browser(found.get("cookies") or [])
                if not cookies.get("remixsid"):
                    continue
                vk.adopt(cookies, user_agent or None)
                try:
                    await vk.refresh_token(force=True)
                except (VKAuthError, aiohttp.ClientError, asyncio.TimeoutError):
                    continue  # вход ещё не завершён (или ВК ответил не сразу)
                return
        raise SystemExit("Вход не выполнен за отведённое время.")
    except (aiohttp.ClientError, ConnectionError):
        raise SystemExit("Окно браузера закрыто до завершения входа.") from None
    finally:
        await local.close()
        _stop(proc)
        shutil.rmtree(profile, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for_browser(http: aiohttp.ClientSession, devtools: str, proc: subprocess.Popen) -> dict:
    """Дождаться, пока браузер поднимет DevTools. -> его /json/version."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STARTUP_TIMEOUT
    while loop.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit("Браузер закрылся сразу после запуска. Попробуйте ещё раз или войдите через --cookie.")
        try:
            async with http.get(devtools + "/json/version", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                info = await resp.json(content_type=None)
            if isinstance(info, dict) and info.get("webSocketDebuggerUrl"):
                return info
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass
        await asyncio.sleep(0.3)
    raise SystemExit("Браузер не запустился. Попробуйте ещё раз или войдите через --cookie.")


async def _has_open_tabs(http: aiohttp.ClientSession, devtools: str) -> bool:
    # На macOS браузер живёт и после закрытия последнего окна — смотрим на вкладки
    async with http.get(devtools + "/json/list", timeout=aiohttp.ClientTimeout(total=5)) as resp:
        tabs = await resp.json(content_type=None)
    return any(isinstance(tab, dict) and tab.get("type") == "page" for tab in tabs)


async def _devtools(ws: aiohttp.ClientWebSocketResponse, method: str) -> dict:
    """Одна команда DevTools Protocol и её результат."""
    await ws.send_json({"id": 1, "method": method})
    async for message in ws:
        if message.type != aiohttp.WSMsgType.TEXT:
            break
        data = json.loads(message.data)
        if data.get("id") == 1:  # остальное — события браузера, они нам не нужны
            return data.get("result") or {}
    raise ConnectionError("браузер закрыл соединение DevTools")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

"""Подключение аккаунта ВК: вход в браузере или импорт cookie.

Пароль программа не видит и не хранит: вы входите на vk.ru сами, а на диск
сохраняются только cookie сессии (data/vk_session.json, права 0600).
"""

from __future__ import annotations

import asyncio
import getpass
import shutil
import tempfile

from .config import Config
from .vk.client import VKAuthError, VKClient
from .vk.cookies import CookieStore

LOGIN_TIMEOUT = 600


async def login(cfg: Config, from_cookie: bool = False) -> None:
    vk = VKClient(cfg)
    if from_cookie:
        vk.cookies = _ask_cookies()
    else:
        browser_cookies, vk.user_agent = await _browser_login(cfg)
        vk.cookies = CookieStore.from_browser(browser_cookies)
    try:
        # Проверяем сессию тем же путём, каким потом будет ходить бот
        await vk.refresh_token(force=True)
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


async def _browser_login(cfg: Config, headless: bool = False) -> tuple[list[dict], str]:
    """Открыть vk.ru в браузере и дождаться входа. -> (cookie браузера, User-Agent)."""
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit(
            "Не установлен playwright: pip install playwright\n"
            "Либо войдите через cookie: python -m vkstream login --cookie"
        ) from None

    # Профиль временный: после входа от него нужны только cookie
    profile = tempfile.mkdtemp(prefix="vkstream-login-")
    try:
        async with async_playwright() as pw:
            context = None
            for channel in ("chrome", "msedge", None):  # None — Chromium самого Playwright
                try:
                    context = await pw.chromium.launch_persistent_context(
                        profile,
                        channel=channel,
                        headless=headless,
                        no_viewport=True,
                        args=["--disable-blink-features=AutomationControlled"],
                        ignore_default_args=["--enable-automation"],
                    )
                    break
                except PlaywrightError:
                    continue
            if context is None:
                raise SystemExit(
                    "Не нашёл браузер. Установите Google Chrome или выполните: playwright install chromium"
                )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(cfg.vk_origin + "/im", wait_until="domcontentloaded")
            print("Войдите в свой аккаунт ВК в открывшемся окне — оно закроется само.")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + LOGIN_TIMEOUT
            while loop.time() < deadline:
                await asyncio.sleep(2)
                try:
                    cookies = await context.cookies()
                    if not any(c["name"] == "remixsid" and c["value"] for c in cookies):
                        continue
                    if not await _has_web_token(context, cfg):
                        continue
                    user_agent = await page.evaluate("navigator.userAgent")
                    cookies = await context.cookies()
                except PlaywrightError:
                    if not context.pages:
                        raise SystemExit("Окно браузера закрыто до завершения входа.") from None
                    continue  # страница перезагружалась в момент проверки
                await context.close()
                return cookies, user_agent
            await context.close()
            raise SystemExit("Вход не выполнен за отведённое время.")
    finally:
        shutil.rmtree(profile, ignore_errors=True)


async def _has_web_token(context, cfg: Config) -> bool:
    """Выдаёт ли VK этой сессии токен веб-клиента, то есть завершён ли вход."""
    resp = await context.request.post(
        f"{cfg.vk_login_base}/?act=web_token",
        form={"version": "1", "app_id": cfg.vk_app_id},
        headers={"Origin": cfg.vk_origin, "Referer": cfg.vk_origin + "/"},
    )
    try:
        payload = await resp.json()
    except ValueError:
        return False
    info = payload.get("data") if isinstance(payload, dict) else None
    return (
        isinstance(info, dict)
        and payload.get("type") == "okay"
        and bool(info.get("access_token"))
        and int(info.get("user_id") or 0) > 0
    )

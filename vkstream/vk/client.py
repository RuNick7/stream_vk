"""HTTP-клиент веб-мессенджера vk.ru.

Протокол тот же, что у сайта vk.ru в браузере:
1. cookie сессии -> POST login.vk.ru/?act=web_token -> короткоживущий access_token;
2. методы API: POST web.api.vk.ru/method/<name>?v=…&client_id=… с этим токеном.

Токены сторонних приложений (Kate Mobile и т.п.) к сообщениям больше не пускают,
поэтому работаем от имени веб-сессии.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import aiohttp

from ..config import Config
from ..state import write_private_json
from .cookies import CookieStore

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

AUTH_ERROR_CODES = {5, 1114, 1116, 1117}
RETRY_ERROR_CODES = {6, 10}  # слишком много запросов / внутренняя ошибка сервера
MIN_CALL_INTERVAL = 0.35  # VK допускает ~3 запроса в секунду


class VKError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class VKAuthError(VKError):
    """Сессии нет или она больше не действует — нужен повторный вход."""

    def __init__(self, message: str, code: int = 5):
        super().__init__(code, message)


@dataclass
class Downloaded:
    data: bytes
    content_type: str


class VKClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cookies = CookieStore()
        self.user_agent = DEFAULT_USER_AGENT
        self.token: str | None = None
        self.token_expires = 0.0
        self.user_id: int | None = None
        self.me_name = ""
        self._http: aiohttp.ClientSession | None = None
        self._session_mtime: float | None = None
        self._refresh_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._last_call = 0.0

    # --- сессия -----------------------------------------------------------

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            connector = None
            if self.cfg.vk_proxy:
                from aiohttp_socks import ProxyConnector

                connector = ProxyConnector.from_url(self.cfg.vk_proxy)
            self._http = aiohttp.ClientSession(
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()

    def session_changed(self) -> bool:
        """Файл сессии появился или был перезаписан (например, командой login)."""
        try:
            return self.cfg.session_path.stat().st_mtime != self._session_mtime
        except FileNotFoundError:
            return False

    def load_session(self) -> None:
        path = self.cfg.session_path
        self._session_mtime = path.stat().st_mtime
        self.token = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.cookies = CookieStore.from_list(data.get("cookies", []))
        except (ValueError, TypeError, AttributeError):
            self.cookies = CookieStore()
            raise VKAuthError("файл сессии ВК повреждён") from None
        self.user_agent = data.get("user_agent") or DEFAULT_USER_AGENT

    def save_session(self) -> None:
        path = self.cfg.session_path
        write_private_json(
            path,
            {
                "user_agent": self.user_agent,
                "saved_at": int(time.time()),
                "cookies": self.cookies.to_list(),
            },
        )
        self._session_mtime = path.stat().st_mtime

    def _headers(self, url: str) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Origin": self.cfg.vk_origin,
            "Referer": self.cfg.vk_origin + "/",
        }
        cookie = self.cookies.header_for(url)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def refresh_token(self, force: bool = False) -> None:
        async with self._refresh_lock:
            # Сессию подхватываем с диска сами: её мог только что записать login
            if self.session_changed():
                self.load_session()
            if not len(self.cookies):
                raise VKAuthError("сессия ВК не сохранена")
            if not force and self.token and time.time() < self.token_expires - 60:
                return
            url = f"{self.cfg.vk_login_base}/?act=web_token"
            async with self.http.post(
                url,
                data={"version": "1", "app_id": self.cfg.vk_app_id},
                headers=self._headers(url),
            ) as resp:
                set_cookie = resp.headers.getall("Set-Cookie", [])
                status = resp.status
                try:
                    payload = await resp.json(content_type=None)
                except ValueError:
                    payload = None
            if not isinstance(payload, dict):
                raise VKError(-1, f"login.vk.ru вернул не JSON (HTTP {status})")
            info = payload.get("data")
            if payload.get("type") != "okay" or not isinstance(info, dict) or not info.get("access_token"):
                self.token = None
                raise VKAuthError(str(payload.get("error_info") or "сессия ВК недействительна"))
            # Set-Cookie применяем только при успехе: на отказ VK отвечает
            # очисткой cookie, и разовый сбой не должен стирать сессию с диска.
            if self.cookies.update_from_headers(url, set_cookie):
                self.save_session()
            now = time.time()
            expires = float(info.get("expires") or 0)
            if expires <= 0:
                expires = now + 3600
            elif expires < 10**9:  # на случай, если придёт время жизни, а не момент
                expires += now
            self.token = info["access_token"]
            self.token_expires = expires
            self.user_id = int(info.get("user_id") or 0) or None

    async def authorize(self) -> None:
        """Получить свежий токен по сохранённой сессии и убедиться, что он рабочий."""
        await self.refresh_token(force=True)
        users = await self.call("users.get")
        if not users:
            raise VKAuthError("ВК не вернул профиль — вход не выполнен")
        me = users[0]
        self.user_id = me.get("id") or self.user_id
        self.me_name = f"{me.get('first_name', '')} {me.get('last_name', '')}".strip()

    # --- API --------------------------------------------------------------

    async def _throttle(self) -> None:
        async with self._rate_lock:
            delay = self._last_call + MIN_CALL_INTERVAL - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_call = time.monotonic()

    async def call(self, method: str, **params: Any) -> Any:
        data = {k: _encode(v) for k, v in params.items() if v is not None}
        url = f"{self.cfg.vk_api_base}/{method}"
        query = {"v": self.cfg.vk_api_version, "client_id": self.cfg.vk_app_id}
        refreshed = False
        error = VKError(-1, "нет ответа")
        for attempt in range(4):
            await self.refresh_token()
            await self._throttle()
            headers = self._headers(url)
            headers["Authorization"] = f"Bearer {self.token}"
            async with self.http.post(
                url, params=query, data={**data, "access_token": self.token}, headers=headers
            ) as resp:
                status = resp.status
                try:
                    payload = await resp.json(content_type=None)
                except ValueError:
                    payload = None
            if not isinstance(payload, dict):
                raise VKError(-1, f"{method}: не JSON (HTTP {status})")
            if "error" not in payload:
                return payload.get("response")
            err = payload["error"] if isinstance(payload["error"], dict) else {}
            code = int(err.get("error_code") or -1)
            message = str(err.get("error_msg") or payload["error"])
            if code in AUTH_ERROR_CODES:
                if refreshed:
                    raise VKAuthError(message, code)
                refreshed = True
                await self.refresh_token(force=True)
                continue
            error = VKError(code, f"{method}: {message}")
            if code in RETRY_ERROR_CODES:
                await asyncio.sleep(1 + attempt)
                continue
            raise error
        raise error

    async def request_json(
        self,
        http_method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        timeout: float = 60,
    ) -> Any:
        """Запрос мимо API (long poll). Пустое тело -> None."""
        async with self.http.request(
            http_method,
            url,
            params=params,
            data=data,
            headers=self._headers(url),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = await resp.text()
            status = resp.status
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise VKError(-1, f"long poll: не JSON (HTTP {status})") from None

    async def download(self, url: str, max_bytes: int) -> Downloaded | None:
        """Скачать вложение. None, если не получилось или файл больше max_bytes."""
        try:
            for _ in range(6):
                # Редиректы обходим вручную, чтобы cookie сессии уходили только на
                # домены VK, а не на CDN, куда ведёт ссылка.
                headers = {"User-Agent": self.user_agent, "Referer": self.cfg.vk_origin + "/"}
                cookie = self.cookies.header_for(url)
                if cookie:
                    headers["Cookie"] = cookie
                async with self.http.get(
                    url,
                    headers=headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and "Location" in resp.headers:
                        url = urljoin(url, resp.headers["Location"])
                        continue
                    if resp.status != 200:
                        log.warning("Вложение не скачалось: HTTP %s", resp.status)
                        return None
                    if (resp.content_length or 0) > max_bytes:
                        return None
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        buf += chunk
                        if len(buf) > max_bytes:
                            return None
                    return Downloaded(bytes(buf), resp.content_type or "")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Вложение не скачалось: %s", e)
        return None


def _encode(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(str(v) for v in value)
    return str(value)

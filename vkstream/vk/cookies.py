"""Мини-хранилище cookie сессии vk.ru.

Своё, а не aiohttp.CookieJar: его нужно хранить в читаемом JSON и точно
контролировать, на какие хосты уходит сессия.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from typing import Iterable, Iterator
from urllib.parse import urlsplit

VK_DOMAINS = ("vk.ru", "vk.com")


@dataclass
class Cookie:
    name: str
    value: str
    domain: str  # без ведущей точки
    path: str = "/"
    expires: float | None = None  # unix-время; None — до конца сессии
    host_only: bool = False

    def expired(self, now: float | None = None) -> bool:
        return self.expires is not None and self.expires <= (now or time.time())

    def matches(self, host: str, path: str) -> bool:
        if host != self.domain and (self.host_only or not host.endswith("." + self.domain)):
            return False
        if path == self.path or self.path == "/":
            return True
        prefix = self.path.rstrip("/")
        return path.startswith(prefix + "/")


def _is_vk_domain(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in VK_DOMAINS)


class CookieStore:
    def __init__(self, cookies: Iterable[Cookie] = ()):
        self._cookies: dict[tuple[str, str, str], Cookie] = {}
        for cookie in cookies:
            self.set(cookie)

    def __len__(self) -> int:
        return len(self._cookies)

    def __iter__(self) -> Iterator[Cookie]:
        return iter(self._cookies.values())

    def set(self, cookie: Cookie) -> bool:
        """Сохранить cookie (пустое значение или истёкший срок — удалить). True, если что-то изменилось."""
        key = (cookie.domain, cookie.path, cookie.name)
        old = self._cookies.get(key)
        if not cookie.value or cookie.value == "DELETED" or cookie.expired():
            return self._cookies.pop(key, None) is not None
        self._cookies[key] = cookie
        return old != cookie

    def get(self, name: str) -> str | None:
        for cookie in self._cookies.values():
            if cookie.name == name and not cookie.expired():
                return cookie.value
        return None

    def header_for(self, url: str) -> str:
        """Значение заголовка Cookie для запроса на url."""
        parts = urlsplit(url)
        host, path = (parts.hostname or "").lower(), parts.path or "/"
        now = time.time()
        return "; ".join(
            f"{c.name}={c.value}"
            for c in self._cookies.values()
            if not c.expired(now) and c.matches(host, path)
        )

    def update_from_headers(self, url: str, set_cookie: Iterable[str]) -> bool:
        """Применить заголовки Set-Cookie ответа на url. True, если что-то изменилось."""
        host = (urlsplit(url).hostname or "").lower()
        changed = False
        for header in set_cookie:
            jar = SimpleCookie()
            try:
                jar.load(header)
            except CookieError:
                continue
            for name, morsel in jar.items():
                domain = morsel["domain"].lstrip(".").lower()
                changed |= self.set(
                    Cookie(
                        name=name,
                        value=morsel.value,
                        domain=domain or host,
                        path=morsel["path"] or "/",
                        expires=_expiry(morsel["max-age"], morsel["expires"]),
                        host_only=not domain,
                    )
                )
        return changed

    def to_list(self) -> list[dict]:
        return [asdict(c) for c in self._cookies.values()]

    @classmethod
    def from_list(cls, items: Iterable[dict]) -> CookieStore:
        return cls(Cookie(**item) for item in items)

    @classmethod
    def from_header(cls, header: str, domain: str = "vk.ru") -> CookieStore:
        """Из строки `a=1; b=2`, скопированной из заголовка Cookie в DevTools."""
        cookies = []
        for part in header.removeprefix("Cookie:").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name:
                cookies.append(Cookie(name=name.strip(), value=value.strip(), domain=domain))
        return cls(cookies)

    @classmethod
    def from_browser(cls, cookies: Iterable[dict]) -> CookieStore:
        """Из cookie браузера (формат Playwright); сторонние домены отбрасываются."""
        result = []
        for c in cookies:
            raw_domain = str(c.get("domain") or "").lower()
            domain = raw_domain.lstrip(".")
            if not _is_vk_domain(domain):
                continue
            expires = c.get("expires")
            result.append(
                Cookie(
                    name=c["name"],
                    value=c.get("value") or "",
                    domain=domain,
                    path=c.get("path") or "/",
                    expires=float(expires) if expires and expires > 0 else None,
                    host_only=not raw_domain.startswith("."),
                )
            )
        return cls(result)


def _expiry(max_age: str, expires: str) -> float | None:
    if max_age:
        try:
            return time.time() + int(max_age)
        except ValueError:
            pass
    if expires:
        try:
            return parsedate_to_datetime(expires.replace("-", " ")).timestamp()
        except (TypeError, ValueError):
            pass
    return None

"""Поддельный сервер ВК (login + API + long poll) и общие фикстуры."""

from __future__ import annotations

import asyncio
import datetime
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, GetUpdates, SendMediaGroup, TelegramMethod
from aiogram.types import Chat, Message, User
from aiohttp import web
from aiohttp.test_utils import TestServer

from vkstream.config import Config
from vkstream.render import Rendered
from vkstream.state import write_private_json
from vkstream.vk import client as client_module

CHAT = 2_000_000_001
USER = 42


class FakeVK:
    def __init__(self) -> None:
        self.base = ""
        self.session_ok = True
        self.reject_tokens = False  # API отвергает любой токен, даже только что выданный
        self.token: str | None = None
        self.logins = 0
        self.calls: list[str] = []
        self.disabled: set[str] = set()
        self.history: dict[int, list[dict]] = {}
        self.titles: dict[int, str] = {CHAT: "Рабочий чат"}
        self.lp_updates: asyncio.Queue = asyncio.Queue()
        self.lp_requests: list[str] = []
        self.lp_ts = 100
        self.file_cookies: list[str] = []  # заголовок Cookie каждого запроса за файлом
        self.pages: dict[str, Any] = {}  # доп. страницы: путь -> обработчик

    # --- управление из тестов ---

    def add_message(self, peer_id: int, text: str, from_id: int = USER, out: int = 0, **extra) -> dict:
        messages = self.history.setdefault(peer_id, [])
        cmid = len(messages) + 1
        msg = {
            "id": peer_id % 1000 * 1000 + cmid,
            "conversation_message_id": cmid,
            "peer_id": peer_id,
            "from_id": from_id,
            "date": int(time.time()),
            "out": out,
            "text": text,
            "attachments": [],
            **extra,
        }
        messages.append(msg)
        return msg

    def push_event(self, update: list) -> None:
        self.lp_updates.put_nowait(update)

    def expire_token(self) -> None:
        self.token = "expired-on-server"

    # --- HTTP ---

    async def handle_login(self, request: web.Request) -> web.Response:
        self.logins += 1
        if not self.session_ok or "remixsid=good" not in request.headers.get("Cookie", ""):
            return web.json_response(
                {"type": "error", "error_info": "need login"},
                headers={"Set-Cookie": "remixsid=DELETED; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/"},
            )
        self.token = f"token-{self.logins}"
        return web.json_response(
            {"type": "okay", "data": {"access_token": self.token, "expires": int(time.time()) + 3600, "user_id": 1}}
        )

    async def handle_method(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        form = await request.post()
        self.calls.append(name)
        if self.reject_tokens or form.get("access_token") != self.token:
            return _error(5, "User authorization failed: invalid access_token")
        handler = getattr(self, "m_" + name.replace(".", "_"), None)
        if handler is None or name in self.disabled:
            return _error(3, "Unknown method passed")
        return web.json_response({"response": handler(form)})

    async def handle_lp(self, request: web.Request) -> web.Response:
        self.lp_requests.append(request.method)
        updates = []
        try:
            updates.append(await asyncio.wait_for(self.lp_updates.get(), 0.1))
        except asyncio.TimeoutError:
            pass
        self.lp_ts += 1
        return web.json_response({"ts": self.lp_ts, "updates": updates})

    async def handle_page(self, request: web.Request) -> web.Response:
        handler = self.pages.get(request.path)
        if handler is None:
            raise web.HTTPNotFound()
        return await handler(request)

    async def handle_file(self, request: web.Request) -> web.Response:
        self.file_cookies.append(request.headers.get("Cookie", ""))
        size = int(request.query.get("size", 8))
        return web.Response(body=b"x" * size, content_type="image/jpeg")

    async def handle_redirect(self, request: web.Request) -> web.Response:
        """Как ссылка на документ ВК: редирект на другой хост (CDN)."""
        self.file_cookies.append(request.headers.get("Cookie", ""))
        raise web.HTTPFound(self.base.replace("127.0.0.1", "localhost") + "/file/doc.pdf")

    # --- методы API ---

    def _conversation(self, peer_id: int) -> dict:
        conv = {
            "peer": {"id": peer_id, "type": "chat" if peer_id >= 2_000_000_000 else "user"},
            "last_conversation_message_id": len(self.history.get(peer_id, [])),
        }
        if peer_id in self.titles:
            conv["chat_settings"] = {"title": self.titles[peer_id]}
        return conv

    def _extended(self) -> dict:
        return {
            "profiles": [
                {"id": 1, "first_name": "Я", "last_name": "Сам"},
                {"id": USER, "first_name": "Анна", "last_name": "Смирнова"},
            ],
            "groups": [{"id": 7, "name": "Паблик"}],
        }

    def m_users_get(self, form) -> list:
        return [{"id": 1, "first_name": "Иван", "last_name": "Петров"}]

    def m_messages_getDiff(self, form) -> dict:
        return {"credentials": {"key": "k", "ts": self.lp_ts, "server_lp": f"{self.base}/lp"}}

    def m_messages_getLongPollServer(self, form) -> dict:
        return {"key": "k", "ts": self.lp_ts, "server": f"{self.base}/lp"}

    def m_messages_getConversations(self, form) -> dict:
        peers = sorted(self.history)
        offset, count = int(form.get("offset", 0)), int(form.get("count", 20))
        items = [{"conversation": self._conversation(p)} for p in peers[offset : offset + count]]
        return {"count": len(peers), "items": items, **self._extended()}

    def m_messages_searchConversations(self, form) -> dict:
        query = str(form.get("q", "")).lower()
        found = [p for p in sorted(self.history) if query in self.titles.get(p, "анна смирнова").lower()]
        return {"count": len(found), "items": [self._conversation(p) for p in found], **self._extended()}

    def m_messages_getConversationsById(self, form) -> dict:
        peers = [int(p) for p in str(form["peer_ids"]).split(",")]
        items = [self._conversation(p) for p in peers if p in self.history]
        return {"count": len(items), "items": items, **self._extended()}

    def m_messages_getHistory(self, form) -> dict:
        peer_id, count = int(form["peer_id"]), int(form.get("count", 20))
        messages = self.history.get(peer_id, [])
        return {
            "count": len(messages),
            "items": list(reversed(messages))[:count],
            "conversations": [self._conversation(peer_id)],
            **self._extended(),
        }


def _error(code: int, message: str) -> web.Response:
    return web.json_response({"error": {"error_code": code, "error_msg": message}})


class FakeSink:
    def __init__(self) -> None:
        self.sent: list[Rendered] = []
        self.urls: list[str | None] = []
        self.notices: list[str] = []
        self.fail_next: Exception | None = None

    async def send(self, rendered: Rendered, url: str | None = None) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        self.sent.append(rendered)
        self.urls.append(url)

    async def notice(self, text: str) -> None:
        self.notices.append(text)

    @property
    def texts(self) -> list[str]:
        return [r.text for r in self.sent]


def tg_message(chat_id: int, user_id: int, text: str = "") -> Message:
    return Message(
        message_id=1,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        text=text,
    )


class RecordingSession(BaseSession):
    """Вместо запросов в Telegram складывает вызванные методы в список."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self.errors: list[Exception] = []  # будут брошены по одной на очередные вызовы

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.02)
            return []
        self.requests.append(method)
        if self.errors:
            raise self.errors.pop(0)
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, SendMediaGroup):
            return [tg_message(method.chat_id, 42) for _ in method.media]
        if hasattr(method, "chat_id") and type(method).__name__.startswith("Send"):
            return tg_message(method.chat_id, 42)
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):
        yield b""

    def of(self, kind: type) -> list:
        return [r for r in self.requests if isinstance(r, kind)]


def make_bot() -> tuple[Bot, RecordingSession]:
    session = RecordingSession()
    return Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML")), session


async def wait_until(condition, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("условие не выполнилось за отведённое время")
        await asyncio.sleep(0.01)


def write_session(cfg: Config, value: str = "good") -> None:
    write_private_json(
        cfg.session_path,
        {"cookies": [{"name": "remixsid", "value": value, "domain": "127.0.0.1"}]},
    )


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MIN_CALL_INTERVAL", 0)


@pytest.fixture
async def fake_vk():
    fake = FakeVK()
    app = web.Application()
    app.router.add_post("/login/", fake.handle_login)
    app.router.add_post("/method/{name}", fake.handle_method)
    app.router.add_route("*", "/lp", fake.handle_lp)
    app.router.add_get("/file/{name}", fake.handle_file)
    app.router.add_get("/redirect", fake.handle_redirect)
    app.router.add_get("/{tail:.*}", fake.handle_page)
    server = TestServer(app)
    await server.start_server()
    fake.base = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
def cfg(fake_vk: FakeVK, tmp_path: Path) -> Config:
    config = replace(
        Config(),
        data_dir=tmp_path,
        vk_origin=fake_vk.base,
        vk_login_base=fake_vk.base + "/login",
        vk_api_base=fake_vk.base + "/method",
        poll_interval=30.0,
        fallback_poll_interval=0.05,
    )
    write_session(config)
    return config

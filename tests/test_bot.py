"""Хендлеры бота через настоящий Dispatcher, с подменённой сетью Telegram."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Update, User

from vkstream.bot import App, build_router, chats_keyboard, status_text
from vkstream.forwarder import Forwarder
from vkstream.state import State
from vkstream.vk.client import VKClient
from vkstream.vk.peers import PeerInfo

from .conftest import CHAT, USER, FakeSink, FakeVK, make_bot, tg_message

OWNER, STRANGER = 100, 200


class Harness:
    def __init__(self, app: App):
        self.app = app
        self.dp = Dispatcher()
        self.dp.include_router(build_router())
        self.bot, self.session = make_bot()
        self._update_id = 0

    async def _feed(self, **fields: Any) -> list[TelegramMethod]:
        self.session.requests.clear()
        self._update_id += 1
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, **fields), app=self.app)
        return list(self.session.requests)

    async def say(self, text: str, user_id: int = OWNER) -> list[TelegramMethod]:
        return await self._feed(message=tg_message(user_id, user_id, text))

    async def press(self, data: str, user_id: int = OWNER) -> list[TelegramMethod]:
        callback = CallbackQuery(
            id="1",
            from_user=User(id=user_id, is_bot=False, first_name="U"),
            chat_instance="x",
            data=data,
            message=tg_message(user_id, user_id, "меню"),
        )
        return await self._feed(callback_query=callback)


@pytest.fixture
async def harness(cfg, fake_vk: FakeVK):
    state = State(cfg.state_path)
    vk = VKClient(cfg)
    app = App(cfg, state, vk, Forwarder(cfg, state, vk, FakeSink()))
    yield Harness(app)
    await vk.close()


def buttons(method: TelegramMethod) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in method.reply_markup.inline_keyboard for b in row]


async def test_owner_is_bound_only_by_pairing_code(harness: Harness):
    app = harness.app
    (reply,) = await harness.say("/start", user_id=STRANGER)
    assert "не привязан" in reply.text
    for guess in ("deadbeef0", "привет", app.pair_code[:-1]):
        (reply,) = await harness.say(f"/start {guess}", user_id=STRANGER)
        assert "не привязан" in reply.text and app.state.owner_id is None

    (reply,) = await harness.say(f"/start {app.pair_code}", user_id=OWNER)
    assert app.state.owner_id == OWNER and "Бот привязан" in reply.text
    assert State.load(app.cfg.state_path).owner_id == OWNER

    # код одноразовый: после привязки чужой /start с ним ничего не меняет
    assert await harness.say(f"/start {app.pair_code}", user_id=STRANGER) == []
    assert app.state.owner_id == OWNER


async def test_strangers_get_nothing(harness: Harness, fake_vk: FakeVK):
    harness.app.state.owner_id = OWNER
    fake_vk.add_message(CHAT, "секрет")
    for text in ("/chats", "/list", "/status", "/find чат", "Рабочий"):
        assert await harness.say(text, user_id=STRANGER) == []
    assert await harness.press(f"w:{CHAT}:0", user_id=STRANGER) == []
    assert fake_vk.calls == [] and harness.app.state.watches == {}


async def test_pick_chat_from_list_then_remove(harness: Harness, fake_vk: FakeVK):
    harness.app.state.owner_id = OWNER
    fake_vk.add_message(CHAT, "раз")
    fake_vk.add_message(USER, "два")

    (reply,) = await harness.say("/chats")
    assert isinstance(reply, SendMessage) and "Всего диалогов: 2" in reply.text
    assert buttons(reply) == [
        ("👤 Анна Смирнова", f"w:{USER}:0"),
        ("👥 Рабочий чат", f"w:{CHAT}:0"),
        ("🔄 1/1", "p:0"),
    ]

    edit, answer = await harness.press(f"w:{CHAT}:0")
    assert isinstance(edit, EditMessageText) and ("✅ 👥 Рабочий чат", f"w:{CHAT}:0") in buttons(edit)
    assert isinstance(answer, AnswerCallbackQuery) and answer.text == "Пересылается: Рабочий чат"
    assert harness.app.state.watches[CHAT].last_cmid == 1

    (reply,) = await harness.say("/list")
    assert "Пересылается чатов: 1" in reply.text
    assert buttons(reply) == [("❌ 👥 Рабочий чат", f"r:{CHAT}")]

    edit, answer = await harness.press(f"r:{CHAT}")
    assert "Пока ничего не пересылается" in edit.text and answer.text == "Убрано: Рабочий чат"
    assert harness.app.state.watches == {}


async def test_toggle_twice_unsubscribes(harness: Harness, fake_vk: FakeVK):
    harness.app.state.owner_id = OWNER
    fake_vk.add_message(CHAT, "раз")
    await harness.press(f"w:{CHAT}:0")
    _, answer = await harness.press(f"w:{CHAT}:0")
    assert answer.text == "Больше не пересылается: Рабочий чат"
    assert harness.app.state.watches == {}


async def test_search_by_command_and_by_plain_text(harness: Harness, fake_vk: FakeVK):
    harness.app.state.owner_id = OWNER
    fake_vk.add_message(CHAT, "раз")
    fake_vk.add_message(USER, "два")

    (reply,) = await harness.say("/find рабоч")
    assert buttons(reply) == [("👥 Рабочий чат", f"s:{CHAT}")]
    (reply,) = await harness.say("анна")
    assert buttons(reply) == [("👤 Анна Смирнова", f"s:{USER}")]

    edit, answer = await harness.press(f"s:{USER}")
    assert buttons(edit) == [("✅ 👤 Анна Смирнова", f"s:{USER}")]
    assert answer.text == "Пересылается: Анна Смирнова"

    (reply,) = await harness.say("/find нет такого")
    assert "не найдено" in reply.text and reply.reply_markup is None


async def test_paging(harness: Harness, fake_vk: FakeVK):
    harness.app.state.owner_id = OWNER
    for peer_id in range(1, 21):
        fake_vk.add_message(peer_id, "x")
    (reply,) = await harness.say("/chats")
    assert buttons(reply)[-2:] == [("🔄 1/3", "p:0"), ("▶️", "p:1")]
    edit, _ = await harness.press("p:2")
    assert len(buttons(edit)) == 4 + 2
    assert buttons(edit)[-2:] == [("◀️", "p:1"), ("🔄 3/3", "p:2")]


async def test_vk_problems_are_explained_not_swallowed(harness: Harness, fake_vk: FakeVK):
    harness.app.state.owner_id = OWNER
    harness.app.cfg.session_path.unlink()
    (reply,) = await harness.say("/chats")
    assert "ВК не подключён" in reply.text and "python -m vkstream login" in reply.text
    (answer,) = await harness.press("p:0")
    assert isinstance(answer, AnswerCallbackQuery) and answer.show_alert


async def test_status_text(harness: Harness):
    app = harness.app
    assert "подключаюсь" in status_text(app)
    app.forwarder.auth_ok, app.forwarder.auth_error = False, "сессия ВК не сохранена"
    assert "ВК не подключён: сессия ВК не сохранена" in status_text(app)
    app.forwarder.auth_ok, app.vk.me_name = True, "Иван Петров"
    assert "✅ ВК: Иван Петров" in status_text(app) and "/chats" in status_text(app)


def test_long_titles_are_trimmed_in_buttons():
    markup = chats_keyboard([PeerInfo(1, "Очень длинное название чата " * 5, "chat", 0)], {1}, 0, 1)
    text = markup.inline_keyboard[0][0].text
    assert text.startswith("✅ 👥 Очень длинное") and text.endswith("…") and len(text) < 50

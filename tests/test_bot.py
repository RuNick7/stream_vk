"""Хендлеры бота через настоящий Dispatcher, с подменённой сетью Telegram."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Update, User

from vkstream.bot import App, build_router, category_screen, status_text
from vkstream.forwarder import CHANNELS, CONVERSATIONS, Forwarder
from vkstream.state import ALLOW, ASK, BLOCK, BUSINESS, CHANNEL, GROUP, PENDING, PERSONAL, Chat, State
from vkstream.vk.client import VKClient

from .conftest import FakeSink, make_bot, tg_message

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


CHAT, CHAT2, USER, SHOP, CHANNEL_ID = 2_000_000_001, 2_000_000_002, 42, -7, -500


@pytest.fixture
async def harness(cfg):
    """Бот с уже прочитанным списком чатов; в ВК хендлеры не ходят вовсе."""
    state = State(cfg.state_path)
    state.owner_id = OWNER
    state.baselined.add(CONVERSATIONS)
    for chat in (
        Chat(USER, "Анна Смирнова", PERSONAL, last_cmid=5, head_cmid=5, last_date=400),
        Chat(CHAT, "Рабочий чат", GROUP, last_cmid=9, head_cmid=9, last_date=300),
        Chat(CHAT2, "Соседи", GROUP, last_cmid=1, head_cmid=1, last_date=200),
        Chat(SHOP, "Магазин", BUSINESS, head_cmid=3, last_date=100),
        Chat(CHANNEL_ID, "Новости канала", CHANNEL, api="channels", last_cmid=7, head_cmid=7, last_date=50),
    ):
        state.chats[chat.peer_id] = chat
    vk = VKClient(cfg)
    app = App(cfg, state, vk, Forwarder(cfg, state, vk, FakeSink()))
    yield Harness(app)
    await vk.close()


def buttons(method: TelegramMethod) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in method.reply_markup.inline_keyboard for b in row]


async def test_owner_is_bound_only_by_pairing_code(harness: Harness):
    app = harness.app
    app.state.owner_id = None
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


async def test_strangers_get_nothing_and_change_nothing(harness: Harness):
    for text in ("/settings", "/status", "/find чат", "Рабочий", "/help"):
        assert await harness.say(text, user_id=STRANGER) == []
    for data in (f"t:{CHAT}:0", f"d:{CHAT}:allow", f"f:{USER}", "c:group:0", "m"):
        assert await harness.press(data, user_id=STRANGER) == []
    assert all(chat.decision is None for chat in harness.app.state.chats.values())


async def test_settings_show_rules_and_counts_per_category(harness: Harness):
    harness.app.state.chats[CHAT].decision = ALLOW
    (reply,) = await harness.say("/settings")
    assert isinstance(reply, SendMessage)
    text = reply.text
    assert "👤 <b>Личные</b> — пересылаются все\n      всего 1" in text
    assert "👥 <b>Группы</b> — по выбору\n      ✅ 1 · 🚫 0 · ❓ 1" in text
    assert "📣 <b>Каналы</b> — по выбору\n      ✅ 0 · 🚫 0 · ❓ 1" in text
    assert "🏢 <b>Бизнес и реклама</b> — не пересылаются\n      всего 1" in text
    assert buttons(reply) == [
        ("👥 Группы", "c:group:0"),
        ("📣 Каналы", "c:channel:0"),
        ("👤 Личные", "c:personal:0"),
        ("🏢 Бизнес и реклама", "c:business:0"),
    ]


async def test_change_choice_for_group_in_settings(harness: Harness):
    app = harness.app
    edit, _ = await harness.press("c:group:0")
    assert isinstance(edit, EditMessageText) and "❓ спрошу при первом сообщении" in edit.text
    assert buttons(edit) == [
        ("❓ Рабочий чат", f"t:{CHAT}:0"),
        ("❓ Соседи", f"t:{CHAT2}:0"),
        ("1/1", "c:group:0"),
        ("« Разделы", "m"),
    ]

    edit, answer = await harness.press(f"t:{CHAT}:0")
    assert ("✅ Рабочий чат", f"t:{CHAT}:0") in buttons(edit)
    assert isinstance(answer, AnswerCallbackQuery) and answer.text == "Пересылается: Рабочий чат"
    assert app.state.chats[CHAT].policy == ALLOW

    edit, answer = await harness.press(f"t:{CHAT}:0")
    assert ("🚫 Рабочий чат", f"t:{CHAT}:0") in buttons(edit)
    assert answer.text == "Заблокировано: Рабочий чат"
    assert State.load(app.cfg.state_path).chats[CHAT].decision == BLOCK  # выбор сохранён на диск

    edit, _ = await harness.press("m")
    assert "⚙️ <b>Что пересылать</b>" in edit.text and "✅ 0 · 🚫 1 · ❓ 1" in edit.text


async def test_exceptions_for_personal_and_business_chats(harness: Harness):
    app = harness.app
    edit, _ = await harness.press("c:personal:0")
    assert ("✅ Анна Смирнова", f"t:{USER}:0") in buttons(edit)
    edit, answer = await harness.press(f"t:{USER}:0")
    assert ("🚫 Анна Смирнова", f"t:{USER}:0") in buttons(edit) and app.state.chats[USER].policy == BLOCK

    edit, _ = await harness.press("c:business:0")
    assert ("🚫 Магазин", f"t:{SHOP}:0") in buttons(edit)
    edit, answer = await harness.press(f"t:{SHOP}:0")
    assert ("✅ Магазин", f"t:{SHOP}:0") in buttons(edit) and answer.text == "Пересылается: Магазин"

    (reply,) = await harness.say("/settings")
    assert "всего 1, заблокировано 1" in reply.text and "всего 1, разрешено 1" in reply.text


async def test_answering_the_question_under_first_message(harness: Harness):
    app = harness.app
    chat = app.state.chats[CHAT]
    chat.asked = True
    assert chat.policy == PENDING

    edit, answer = await harness.press(f"d:{CHAT}:allow")
    assert chat.policy == ALLOW and chat.asked is False
    assert edit.text == "✅ 👥 <b>Рабочий чат</b> — пересылается.\nИзменить выбор: /settings"
    assert edit.reply_markup is None and answer.text == "Пересылается: Рабочий чат"

    channel = app.state.chats[CHANNEL_ID]
    channel.asked = True
    edit, answer = await harness.press(f"d:{CHANNEL_ID}:block")
    assert channel.policy == BLOCK
    assert edit.text.startswith("🚫 📣 <b>Новости канала</b> — не пересылается.")
    assert answer.text == "Заблокировано: Новости канала"


async def test_search_by_command_and_by_plain_text(harness: Harness):
    (reply,) = await harness.say("/find РАБОЧ")
    assert buttons(reply) == [("❓ 👥 Рабочий чат", f"f:{CHAT}")]
    (reply,) = await harness.say("анна")
    assert buttons(reply) == [("✅ 👤 Анна Смирнова", f"f:{USER}")]

    edit, answer = await harness.press(f"f:{USER}")
    assert buttons(edit) == [("🚫 👤 Анна Смирнова", f"f:{USER}")]
    assert answer.text == "Заблокировано: Анна Смирнова"

    (reply,) = await harness.say("о")  # находит чаты всех категорий, свежие — выше
    assert [text for text, _ in buttons(reply)] == [
        "🚫 👤 Анна Смирнова",
        "❓ 👥 Рабочий чат",
        "❓ 👥 Соседи",
        "❓ 📣 Новости канала",
    ]
    (reply,) = await harness.say("/find нет такого")
    assert "не найдено" in reply.text and reply.reply_markup is None
    (reply,) = await harness.say("/find")
    assert "Напишите, что искать" in reply.text


async def test_category_paging_and_long_titles(harness: Harness):
    app = harness.app
    for i in range(20):
        app.state.chats[3000 + i] = Chat(3000 + i, f"Друг {i:02d}", PERSONAL, last_date=1000 - i)
    app.state.chats[3000].title = "Очень длинное имя собеседника " * 4
    edit, _ = await harness.press("c:personal:0")
    first = buttons(edit)
    assert len(first) == 8 + 2 + 1
    assert first[0][0].startswith("✅ Очень длинное") and first[0][0].endswith("…") and len(first[0][0]) < 45
    assert first[-3:] == [("1/3", "c:personal:0"), ("▶️", "c:personal:1"), ("« Разделы", "m")]

    edit, _ = await harness.press("c:personal:2")
    assert buttons(edit)[-3:] == [("◀️", "c:personal:1"), ("3/3", "c:personal:2"), ("« Разделы", "m")]
    assert ("✅ Анна Смирнова", f"t:{USER}:2") in buttons(edit)  # самая давняя переписка — в конце

    text, markup = category_screen(app, PERSONAL, 99)  # страница за пределами списка
    assert markup.inline_keyboard[-2][-1].text == "3/3"


async def test_settings_before_chat_list_is_read(harness: Harness):
    harness.app.state.baselined.clear()
    (reply,) = await harness.say("/settings")
    assert "ещё не прочитан" in reply.text and reply.reply_markup is None


async def test_status_text(harness: Harness):
    app, fwd = harness.app, harness.app.forwarder
    assert "подключаюсь" in status_text(app)
    fwd.auth_ok, fwd.auth_error = False, "сессия ВК не сохранена"
    assert "ВК не подключён: сессия ВК не сохранена" in status_text(app)
    assert "python -m vkstream login" in status_text(app)

    fwd.auth_ok, app.vk.me_name, fwd.lp_mode = True, "Иван Петров", "web"
    text = status_text(app)
    assert "✅ ВК: Иван Петров" in text and "мгновенно" in text
    assert "👥 Группы: ✅ 0 · 🚫 0 · ❓ 2" in text and "⚠️" not in text

    fwd.lp_mode = None
    fwd.feature_errors[CHANNELS] = "[3] channels.get: Unknown method passed"
    fwd.read_errors["messages"] = "[917] нет доступа"
    text = status_text(app)
    assert "опрос раз в" in text
    assert "⚠️ Каналы недоступны: [3] channels.get: Unknown method passed" in text
    assert "⚠️ Последняя ошибка чтения сообщений: [917] нет доступа" in text

    edit, _ = await harness.press("c:channel:0")
    assert "Нажмите на чат" in edit.text
    del app.state.chats[CHANNEL_ID]
    edit, _ = await harness.press("c:channel:0")
    assert "ВК не отдал список каналов" in edit.text


# --- добавление сообщества по ссылке ------------------------------------------


async def test_add_community_by_command_and_by_plain_link(harness: Harness, fake_vk):
    app = harness.app
    fake_vk.add_community()
    fake_vk.add_community(130, "Конспекты", "notes")
    fake_vk.add_wall_post("уже было")

    (reply,) = await harness.say("/add https://vk.ru/club129")
    assert "✅ 📣 <b>Лекции</b> добавлено." in reply.text and "раз в 5 мин" in reply.text
    assert "groups.getById" not in fake_vk.calls  # хватило одного чтения стены
    (reply,) = await harness.say("https://vk.ru/notes")  # ссылку можно прислать и без команды
    assert "<b>Конспекты</b> добавлено" in reply.text
    (reply,) = await harness.say("/add lectures")  # повторное добавление ничего не ломает
    assert "<b>Лекции</b> добавлено" in reply.text

    walls = [c for c in app.state.chats.values() if c.api == "wall"]
    assert sorted((c.title, c.kind, c.policy) for c in walls) == [
        ("Конспекты", CHANNEL, ALLOW),
        ("Лекции", CHANNEL, ALLOW),
    ]
    edit, _ = await harness.press("c:channel:0")
    titles = [text for text, _ in buttons(edit)]
    assert "✅ Лекции" in titles and "✅ Конспекты" in titles and "/add" in edit.text


async def test_add_explains_what_went_wrong(harness: Harness, fake_vk):
    fake_vk.add_community()
    (reply,) = await harness.say("/add")
    assert "Пришлите ссылку" in reply.text
    (reply,) = await harness.say("/add https://vk.ru/durov")
    assert "не похоже на сообщество ВК (это user)" in reply.text
    (reply,) = await harness.say("https://vk.ru/no_such_page")
    assert "не похоже на сообщество ВК (такой страницы нет)" in reply.text
    (reply,) = await harness.say("/add club999")  # ответ ВК виден дословно
    assert "ВК не дал прочитать стену" in reply.text and "[100] wall.get:" in reply.text

    fake_vk.communities[129]["closed"] = True
    (reply,) = await harness.say("/add club129")
    assert "закрыта для вашего аккаунта" in reply.text and "[15] wall.get: Access denied" in reply.text

    harness.app.cfg.session_path.unlink()
    harness.app.vk.token = None
    harness.app.vk.cookies = type(harness.app.vk.cookies)()
    (reply,) = await harness.say("/add club129")
    assert "ВК не подключён" in reply.text and "python -m vkstream login" in reply.text
    assert all(c.api != "wall" for c in harness.app.state.chats.values())


async def test_plain_words_still_search_not_add(harness: Harness, fake_vk):
    fake_vk.add_community(screen_name="soseди")
    (reply,) = await harness.say("Соседи")
    assert buttons(reply) == [("❓ 👥 Соседи", f"f:{CHAT2}")]
    assert "utils.resolveScreenName" not in fake_vk.calls

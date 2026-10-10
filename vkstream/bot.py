"""Интерфейс бота: настройки пересылки по категориям и чатам, поиск, статус."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

import aiohttp
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from .config import Config
from .forwarder import CHANNELS, CONVERSATIONS, LOGIN_HINT, REQUESTS, Forwarder
from .render import KIND_ICONS, esc
from .state import ALLOW, BLOCK, BUSINESS, CHANNEL, GROUP, PERSONAL, Chat, State
from .vk.client import VKAuthError, VKClient, VKError
from .vk.wall import NotACommunity, parse_link

PAGE_SIZE = 8
SEARCH_LIMIT = 10
TITLE_LIMIT = 40

KIND_NAMES = {PERSONAL: "Личные", GROUP: "Группы", CHANNEL: "Каналы", BUSINESS: "Бизнес и реклама"}
KIND_RULES = {
    PERSONAL: "пересылаются все",
    GROUP: "по выбору",
    CHANNEL: "по выбору",
    BUSINESS: "не пересылаются",
}
KIND_LEGEND = {
    PERSONAL: "✅ пересылается · 🚫 заблокирован",
    GROUP: "✅ пересылается · 🚫 заблокирована · ❓ спрошу при первом сообщении",
    CHANNEL: "✅ пересылается · 🚫 заблокирован · ❓ спрошу при первом посте\n"
    "Сообщество со стеной добавляется ссылкой: /add",
    BUSINESS: "✅ пересылается · 🚫 не пересылается",
}
# Порядок разделов на экране настроек
KIND_ORDER = (GROUP, CHANNEL, PERSONAL, BUSINESS)

# Коды VK API «нет доступа»: закрытая группа, приватная стена, вас заблокировали
ACCESS_DENIED_CODES = {15, 18, 30, 203}

COMMANDS = [
    BotCommand(command="settings", description="Что пересылать: разделы и чаты"),
    BotCommand(command="add", description="Добавить сообщество по ссылке"),
    BotCommand(command="find", description="Найти чат по названию"),
    BotCommand(command="status", description="Состояние подключения к ВК"),
    BotCommand(command="help", description="Справка"),
]

HELP = (
    "Я пересылаю сюда новые сообщения из ВКонтакте:\n\n"
    "👤 <b>личные</b> — все, автоматически;\n"
    "👥 <b>группы</b> и 📣 <b>каналы</b> — по вашему выбору: при первом сообщении "
    "спрошу, оставить или заблокировать;\n"
    "🏢 <b>бизнес и реклама</b> (диалоги с сообществами) — не пересылаю.\n\n"
    "/settings — изменить выбор для любого чата\n"
    "/add <i>ссылка</i> — пересылать новые записи со стены сообщества "
    "(или просто пришлите ссылку вида vk.ru/club1)\n"
    "/find <i>текст</i> — найти чат по названию (или просто напишите название)\n"
    "/status — состояние подключения к ВК"
)


@dataclass
class App:
    cfg: Config
    state: State
    vk: VKClient
    forwarder: Forwarder
    # Одноразовый код привязки владельца, пока он не задан
    pair_code: str = field(default_factory=lambda: secrets.token_hex(4))
    last_query: str | None = None


class IsOwner(BaseFilter):
    async def __call__(self, event: TelegramObject, app: App) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and app.state.owner_id == user.id


# --- оформление ---------------------------------------------------------------


def status_icon(chat: Chat) -> str:
    return {ALLOW: "✅", BLOCK: "🚫"}.get(chat.policy, "❓")


def _clip(title: str) -> str:
    return title if len(title) <= TITLE_LIMIT else title[: TITLE_LIMIT - 1] + "…"


def _counts(app: App, kind: str) -> tuple[int, int, int]:
    """Сколько чатов категории: пересылается, заблокировано, ещё без решения."""
    allow = block = ask = 0
    for chat in app.state.chats.values():
        if chat.kind != kind:
            continue
        if chat.policy == ALLOW:
            allow += 1
        elif chat.policy == BLOCK:
            block += 1
        else:
            ask += 1
    return allow, block, ask


def _summary(app: App, kind: str) -> str:
    allow, block, ask = _counts(app, kind)
    if kind == PERSONAL:
        return f"всего {allow + block}" + (f", заблокировано {block}" if block else "")
    if kind == BUSINESS:
        return f"всего {allow + block}" + (f", разрешено {allow}" if allow else "")
    return f"✅ {allow} · 🚫 {block} · ❓ {ask}"


def _decision_text(chat: Chat) -> str:
    verb = "пересылается" if chat.policy == ALLOW else "не пересылается"
    return f"{status_icon(chat)} {KIND_ICONS.get(chat.kind, '')} <b>{esc(chat.title)}</b> — {verb}."


def _toast(chat: Chat) -> str:
    return ("Пересылается: " if chat.policy == ALLOW else "Заблокировано: ") + chat.title


# --- экраны -------------------------------------------------------------------


def settings_screen(app: App) -> tuple[str, InlineKeyboardMarkup | None]:
    if CONVERSATIONS not in app.state.baselined:
        return "Список чатов ВК ещё не прочитан. Что происходит — в /status", None
    lines = ["⚙️ <b>Что пересылать</b>", ""]
    for kind in (PERSONAL, GROUP, CHANNEL, BUSINESS):
        lines.append(f"{KIND_ICONS[kind]} <b>{KIND_NAMES[kind]}</b> — {KIND_RULES[kind]}")
        lines.append(f"      {_summary(app, kind)}")
    lines += ["", "Откройте раздел, чтобы изменить выбор для конкретного чата."]
    buttons = [
        InlineKeyboardButton(text=f"{KIND_ICONS[kind]} {KIND_NAMES[kind]}", callback_data=f"c:{kind}:0")
        for kind in KIND_ORDER
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])


def category_screen(app: App, kind: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    chats = app.state.of_kind(kind)
    pages = max(1, -(-len(chats) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{status_icon(chat)} {_clip(chat.title)}", callback_data=f"t:{chat.peer_id}:{page}"
            )
        ]
        for chat in chats[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"c:{kind}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"c:{kind}:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"c:{kind}:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="« Разделы", callback_data="m")])
    text = f"{KIND_ICONS[kind]} <b>{KIND_NAMES[kind]}</b> — {KIND_RULES[kind]}\n"
    if chats:
        text += f"{KIND_LEGEND[kind]}\nНажмите на чат, чтобы переключить."
    elif kind == CHANNEL and app.forwarder.feature_errors[CHANNELS]:
        text += f"⚠️ ВК не отдал список каналов: {esc(app.forwarder.feature_errors[CHANNELS])}"
    else:
        text += "Здесь пока пусто."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def search_screen(app: App, query: str) -> tuple[str, InlineKeyboardMarkup | None]:
    needle = query.casefold()
    found = [c for c in app.state.chats.values() if needle in c.title.casefold()]
    found.sort(key=lambda c: (-c.last_date, c.title.casefold()))
    if not found:
        return f"По запросу «{esc(query)}» чатов не найдено.", None
    rows = [
        [
            InlineKeyboardButton(
                text=f"{status_icon(chat)} {KIND_ICONS.get(chat.kind, '')} {_clip(chat.title)}",
                callback_data=f"f:{chat.peer_id}",
            )
        ]
        for chat in found[:SEARCH_LIMIT]
    ]
    more = f" (показаны первые {SEARCH_LIMIT} из {len(found)})" if len(found) > SEARCH_LIMIT else ""
    text = f"Найдено по запросу «{esc(query)}»{more}. Нажмите на чат, чтобы переключить пересылку."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def status_text(app: App) -> str:
    fwd = app.forwarder
    if fwd.auth_ok is None:
        return "⏳ ВК: подключаюсь…"
    if not fwd.auth_ok:
        return f"⚠️ ВК не подключён: {esc(fwd.auth_error)}.\n{LOGIN_HINT}"
    lines = [f"✅ ВК: {esc(app.vk.me_name)}"]
    if CONVERSATIONS not in app.state.baselined:
        lines.append("⏳ Читаю список чатов…")
        return "\n".join(lines)
    if fwd.lp_mode:
        lines.append(f"📡 Новые сообщения: мгновенно (long poll, режим {fwd.lp_mode})")
    else:
        lines.append(f"📡 Новые сообщения: опрос раз в {app.cfg.fallback_poll_interval:g} с")
    for kind in (PERSONAL, GROUP, CHANNEL, BUSINESS):
        lines.append(f"{KIND_ICONS[kind]} {KIND_NAMES[kind]}: {_summary(app, kind)}")
    if fwd.feature_errors[CHANNELS]:
        lines.append(f"⚠️ Каналы недоступны: {esc(fwd.feature_errors[CHANNELS])}")
    if fwd.feature_errors[REQUESTS]:
        lines.append(f"⚠️ Запросы на переписку недоступны: {esc(fwd.feature_errors[REQUESTS])}")
    for api, error in fwd.read_errors.items():
        if error:
            what = "постов каналов" if api == CHANNELS else "сообщений"
            lines.append(f"⚠️ Последняя ошибка чтения {what}: {esc(error)}")
    lines.append(f"📨 Переслано с запуска: {fwd.forwarded}")
    if fwd.last_forward_at:
        lines.append("🕒 Последнее: " + time.strftime("%d.%m %H:%M:%S", time.localtime(fwd.last_forward_at)))
    return "\n".join(lines)


async def _edit(message: Message | None, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


# --- хендлеры -----------------------------------------------------------------


async def cmd_start(message: Message, command: CommandObject, app: App) -> None:
    user_id = message.from_user.id if message.from_user else None
    if app.state.owner_id is None:
        code = (command.args or "").strip().encode()
        if user_id and secrets.compare_digest(code, app.pair_code.encode()):
            app.state.owner_id = user_id
            app.state.save()
            await message.answer("✅ Бот привязан к вам.\n\n" + HELP)
        else:
            await message.answer(
                "Бот ещё не привязан к владельцу. Откройте ссылку, которую он "
                "напечатал в консоли при запуске."
            )
    elif user_id == app.state.owner_id:
        await message.answer(HELP)


async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


async def cmd_status(message: Message, app: App) -> None:
    await message.answer(status_text(app))


async def cmd_settings(message: Message, app: App) -> None:
    text, markup = settings_screen(app)
    await message.answer(text, reply_markup=markup)


async def cmd_find(message: Message, command: CommandObject, app: App) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напишите, что искать: <code>/find название чата</code>")
        return
    await _search(message, app, query)


async def cmd_add(message: Message, command: CommandObject, app: App) -> None:
    link = (command.args or "").strip()
    if not link:
        await message.answer("Пришлите ссылку на сообщество: <code>/add https://vk.ru/club1</code>")
        return
    await _add_wall(message, app, link)


async def on_text(message: Message, app: App) -> None:
    text = (message.text or "").strip()
    if parse_link(text):  # ссылка на сообщество — добавляем, всё остальное — поиск
        await _add_wall(message, app, text)
    else:
        await _search(message, app, text)


async def _add_wall(message: Message, app: App, link: str) -> None:
    try:
        chat = await app.forwarder.add_wall(link)
    except NotACommunity:
        text = (
            "Это не похоже на сообщество ВК. Нужна ссылка на группу или паблик, "
            "например <code>https://vk.ru/club1</code>."
        )
    except VKAuthError as e:
        text = f"⚠️ ВК не подключён: {esc(e.message)}.\n{LOGIN_HINT}"
    except VKError as e:
        if e.code in ACCESS_DENIED_CODES:
            text = "⚠️ Стена этого сообщества закрыта для вашего аккаунта ВК."
        else:
            text = f"⚠️ Ошибка VK API: {esc(e)}"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        text = "⚠️ Нет связи с ВК, попробуйте ещё раз."
    else:
        minutes = max(1, round(app.cfg.wall_poll_interval / 60))
        text = (
            f"✅ 📣 <b>{esc(chat.title)}</b> добавлено.\n"
            f"Новые записи со стены будут приходить сюда (проверяю раз в {minutes} мин).\n"
            "Отключить: /settings → Каналы."
        )
    await message.answer(text)


async def _search(message: Message, app: App, query: str) -> None:
    app.last_query = query
    text, markup = search_screen(app, query)
    await message.answer(text, reply_markup=markup)


async def cb_main(callback: CallbackQuery, app: App) -> None:
    text, markup = settings_screen(app)
    await _edit(callback.message, text, markup)
    await callback.answer()


async def cb_category(callback: CallbackQuery, app: App) -> None:
    _, kind, page = callback.data.split(":")
    if kind not in KIND_NAMES:
        await callback.answer()
        return
    text, markup = category_screen(app, kind, int(page))
    await _edit(callback.message, text, markup)
    await callback.answer()


async def cb_toggle(callback: CallbackQuery, app: App) -> None:
    """Переключить чат в списке раздела."""
    _, peer_id, page = callback.data.split(":")
    chat = app.forwarder.toggle(int(peer_id))
    if chat is None:
        await callback.answer("Чат не найден")
        return
    text, markup = category_screen(app, chat.kind, int(page))
    await _edit(callback.message, text, markup)
    await callback.answer(_toast(chat))


async def cb_found_toggle(callback: CallbackQuery, app: App) -> None:
    """Переключить чат в результатах поиска."""
    chat = app.forwarder.toggle(int(callback.data.split(":")[1]))
    if chat is None:
        await callback.answer("Чат не найден")
        return
    if app.last_query:
        text, markup = search_screen(app, app.last_query)
        await _edit(callback.message, text, markup)
    await callback.answer(_toast(chat))


async def cb_decide(callback: CallbackQuery, app: App) -> None:
    """Ответ на вопрос «оставить или заблокировать?» под первым сообщением чата."""
    _, peer_id, decision = callback.data.split(":")
    chat = app.forwarder.decide(int(peer_id), ALLOW if decision == ALLOW else BLOCK)
    if chat is None:
        await callback.answer("Чат не найден")
        return
    await _edit(callback.message, _decision_text(chat) + "\nИзменить выбор: /settings", None)
    await callback.answer(_toast(chat))


def build_router() -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(cmd_start, CommandStart())

    # Всё остальное — только владельцу; чужие сообщения молча игнорируются
    owner = Router()
    owner.message.filter(IsOwner())
    owner.callback_query.filter(IsOwner())
    owner.message.register(cmd_help, Command("help"))
    owner.message.register(cmd_status, Command("status"))
    owner.message.register(cmd_settings, Command("settings"))
    owner.message.register(cmd_find, Command("find"))
    owner.message.register(cmd_add, Command("add"))
    owner.message.register(on_text, F.text & ~F.text.startswith("/"))
    owner.callback_query.register(cb_main, F.data == "m")
    owner.callback_query.register(cb_category, F.data.startswith("c:"))
    owner.callback_query.register(cb_toggle, F.data.startswith("t:"))
    owner.callback_query.register(cb_found_toggle, F.data.startswith("f:"))
    owner.callback_query.register(cb_decide, F.data.startswith("d:"))
    router.include_router(owner)
    return router

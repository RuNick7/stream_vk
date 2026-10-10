"""Интерфейс бота: выбор чатов ВК, список пересылаемых, статус."""

from __future__ import annotations

import asyncio
import functools
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
from .forwarder import LOGIN_HINT, Forwarder
from .render import esc
from .state import State, Watch
from .vk.client import VKAuthError, VKClient, VKError
from .vk.peers import PeerInfo, list_conversations, search_conversations

PAGE_SIZE = 8
SEARCH_LIMIT = 10
TITLE_LIMIT = 40
ICONS = {"user": "👤", "chat": "👥", "group": "📢", "email": "✉️"}

COMMANDS = [
    BotCommand(command="chats", description="Выбрать чаты ВК для пересылки"),
    BotCommand(command="find", description="Найти чат ВК по названию"),
    BotCommand(command="list", description="Что сейчас пересылается"),
    BotCommand(command="status", description="Состояние подключения к ВК"),
    BotCommand(command="help", description="Справка"),
]

HELP = (
    "Я пересылаю сюда новые сообщения из выбранных чатов ВКонтакте.\n\n"
    "/chats — выбрать чаты из списка диалогов\n"
    "/find <i>текст</i> — найти чат по названию (или просто напишите название)\n"
    "/list — что пересылается сейчас\n"
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


def vk_guard(handler):
    """Показать пользователю ошибку ВК вместо молчаливого падения хендлера."""

    @functools.wraps(handler)
    async def wrapper(event: Message | CallbackQuery, *args, **kwargs):
        try:
            return await handler(event, *args, **kwargs)
        except VKAuthError as e:
            alert = "ВК не подключён. Подробности: /status"
            text = f"⚠️ ВК не подключён: {esc(e.message)}.\n{LOGIN_HINT}"
        except VKError as e:
            alert = f"Ошибка VK API: {e}"
            text = "⚠️ " + esc(alert)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            alert = "Нет связи с ВК, попробуйте ещё раз."
            text = "⚠️ " + alert
        if isinstance(event, CallbackQuery):
            await event.answer(alert[:200], show_alert=True)
        else:
            await event.answer(text)

    return wrapper


# --- клавиатуры ---------------------------------------------------------------


def _button_title(title: str, kind: str, watched: bool) -> str:
    title = title if len(title) <= TITLE_LIMIT else title[: TITLE_LIMIT - 1] + "…"
    return f"{'✅ ' if watched else ''}{ICONS.get(kind, '💬')} {title}"


def chats_keyboard(
    items: list[PeerInfo], watched: set[int], page: int, pages: int
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_button_title(p.title, p.kind, p.peer_id in watched),
                callback_data=f"w:{p.peer_id}:{page}",
            )
        ]
        for p in items
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"p:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"🔄 {page + 1}/{pages}", callback_data=f"p:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"p:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_keyboard(items: list[PeerInfo], watched: set[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_button_title(p.title, p.kind, p.peer_id in watched),
                    callback_data=f"s:{p.peer_id}",
                )
            ]
            for p in items
        ]
    )


def watches_keyboard(watches: list[Watch]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ " + _button_title(w.title, w.kind, False),
                    callback_data=f"r:{w.peer_id}",
                )
            ]
            for w in watches
        ]
    )


# --- экраны -------------------------------------------------------------------


async def chats_screen(app: App, page: int) -> tuple[str, InlineKeyboardMarkup]:
    items, total = await list_conversations(app.vk, app.forwarder.names, page * PAGE_SIZE, PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    if not items and page > 0:  # список стал короче, пока листали
        return await chats_screen(app, pages - 1)
    text = (
        "Выберите чаты ВК — новые сообщения из них будут приходить сюда.\n"
        f"✅ — уже пересылается. Всего диалогов: {total}."
    )
    return text, chats_keyboard(items, set(app.state.watches), page, pages)


async def search_screen(app: App, query: str) -> tuple[str, InlineKeyboardMarkup | None]:
    items = await search_conversations(app.vk, app.forwarder.names, query, SEARCH_LIMIT)
    if not items:
        return f"По запросу «{esc(query)}» чатов не найдено.", None
    return f"Найдено по запросу «{esc(query)}»:", search_keyboard(items, set(app.state.watches))


def list_screen(app: App) -> tuple[str, InlineKeyboardMarkup | None]:
    watches = list(app.state.watches.values())
    if not watches:
        return "Пока ничего не пересылается. Выберите чаты: /chats", None
    return f"Пересылается чатов: {len(watches)}. Нажмите, чтобы убрать.", watches_keyboard(watches)


def status_text(app: App) -> str:
    fwd = app.forwarder
    if fwd.auth_ok:
        vk = f"✅ ВК: {esc(app.vk.me_name)}"
    elif fwd.auth_ok is None:
        vk = "⏳ ВК: подключаюсь…"
    else:
        vk = f"⚠️ ВК не подключён: {esc(fwd.auth_error)}.\n{LOGIN_HINT}"
    lines = [vk]
    if fwd.auth_ok:
        if not app.state.watches:
            lines.append("📡 Жду, пока вы выберете чаты: /chats")
        elif fwd.lp_mode:
            lines.append(f"📡 Новые сообщения: мгновенно (long poll, режим {fwd.lp_mode})")
        else:
            lines.append(f"📡 Новые сообщения: опрос раз в {app.cfg.fallback_poll_interval:g} с")
    lines.append(f"💬 Чатов пересылается: {len(app.state.watches)}")
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


async def _toggle(app: App, peer_id: int) -> str:
    removed = app.forwarder.remove_watch(peer_id)
    if removed is not None:
        return f"Больше не пересылается: {removed.title}"
    watch = await app.forwarder.add_watch(peer_id)
    return f"Пересылается: {watch.title}"


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


@vk_guard
async def cmd_chats(message: Message, app: App) -> None:
    text, markup = await chats_screen(app, 0)
    await message.answer(text, reply_markup=markup)


async def cmd_list(message: Message, app: App) -> None:
    text, markup = list_screen(app)
    await message.answer(text, reply_markup=markup)


@vk_guard
async def cmd_find(message: Message, command: CommandObject, app: App) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напишите, что искать: <code>/find название чата</code>")
        return
    await _search(message, app, query)


@vk_guard
async def on_text(message: Message, app: App) -> None:
    await _search(message, app, (message.text or "").strip())


async def _search(message: Message, app: App, query: str) -> None:
    app.last_query = query
    text, markup = await search_screen(app, query)
    await message.answer(text, reply_markup=markup)


@vk_guard
async def cb_page(callback: CallbackQuery, app: App) -> None:
    text, markup = await chats_screen(app, int(callback.data.split(":")[1]))
    await _edit(callback.message, text, markup)
    await callback.answer()


@vk_guard
async def cb_toggle(callback: CallbackQuery, app: App) -> None:
    _, peer_id, page = callback.data.split(":")
    toast = await _toggle(app, int(peer_id))
    text, markup = await chats_screen(app, int(page))
    await _edit(callback.message, text, markup)
    await callback.answer(toast)


@vk_guard
async def cb_search_toggle(callback: CallbackQuery, app: App) -> None:
    toast = await _toggle(app, int(callback.data.split(":")[1]))
    if app.last_query:
        text, markup = await search_screen(app, app.last_query)
        await _edit(callback.message, text, markup)
    await callback.answer(toast)


async def cb_remove(callback: CallbackQuery, app: App) -> None:
    removed = app.forwarder.remove_watch(int(callback.data.split(":")[1]))
    text, markup = list_screen(app)
    await _edit(callback.message, text, markup)
    await callback.answer(f"Убрано: {removed.title}" if removed else "Уже убрано")


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
    owner.message.register(cmd_chats, Command("chats"))
    owner.message.register(cmd_list, Command("list"))
    owner.message.register(cmd_find, Command("find"))
    owner.message.register(on_text, F.text & ~F.text.startswith("/"))
    owner.callback_query.register(cb_page, F.data.startswith("p:"))
    owner.callback_query.register(cb_toggle, F.data.startswith("w:"))
    owner.callback_query.register(cb_search_toggle, F.data.startswith("s:"))
    owner.callback_query.register(cb_remove, F.data.startswith("r:"))
    router.include_router(owner)
    return router

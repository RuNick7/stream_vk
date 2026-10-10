"""Отправка оформленных сообщений владельцу бота в Telegram."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Awaitable, Callable, TypeVar

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
)

from .render import KIND_ICONS, MAX_UPLOAD_BYTES, Media, Rendered, esc, strip_tags
from .state import ALLOW, BLOCK, CHANNEL, Chat, State
from .vk.client import VKClient

log = logging.getLogger(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
PHOTO_LIMIT = 10 * 1024 * 1024
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

T = TypeVar("T")


class TransientSendError(Exception):
    """Telegram временно недоступен — сообщение нужно отправить позже."""


class TelegramSink:
    def __init__(self, bot: Bot, state: State, vk: VKClient):
        self.bot = bot
        self.state = state
        self.vk = vk

    async def notice(self, text: str) -> None:
        """Служебное сообщение владельцу."""
        if self.state.owner_id is not None:
            await self._send_text(self.state.owner_id, text, None)

    async def ask(self, chat: Chat) -> None:
        """Спросить владельца, пересылать ли чат; ответ обрабатывает бот (callback `d:`)."""
        if self.state.owner_id is None:
            return
        source = "этого канала" if chat.kind == CHANNEL else "этой группы"
        text = (
            f"❓ {KIND_ICONS.get(chat.kind, '')} <b>{esc(chat.title)}</b>\n"
            f"Пересылать сюда сообщения из {source}? Пока не ответите, новые придержу."
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Оставить", callback_data=f"d:{chat.peer_id}:{ALLOW}"),
                    InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"d:{chat.peer_id}:{BLOCK}"),
                ]
            ]
        )
        try:
            await self._call(lambda: self.bot.send_message(self.state.owner_id, text, reply_markup=markup))
        except TelegramAPIError as e:
            # Повтор не поможет; выбор всё равно можно сделать в /settings
            log.error("Telegram отклонил вопрос о чате: %s", e)

    async def send(self, rendered: Rendered, url: str | None = None) -> None:
        chat_id = self.state.owner_id
        if chat_id is None:
            return
        markup = None
        if url:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть в VK", url=url)]]
            )
        text = rendered.text
        files: list[tuple[Media, BufferedInputFile]] = []
        for media in rendered.media:
            file = await self._fetch(media)
            if file is None:
                href = html.escape(media.url, quote=True)
                name = esc(media.filename or "вложение")
                text += f'\n📎 <a href="{href}">{name}</a> (не удалось загрузить)'
            else:
                files.append((media, file))

        try:
            if len(files) == 1 and len(strip_tags(text)) <= CAPTION_LIMIT:
                await self._send_media(chat_id, *files[0], caption=text, markup=markup)
                return
            await self._send_text(chat_id, text, markup)
            photos = [f for f in files if f[0].kind == "photo"]
            if len(photos) > 1:
                for i in range(0, len(photos), 10):
                    await self._send_album(chat_id, photos[i : i + 10])
                files = [f for f in files if f[0].kind != "photo"]
            for media, file in files:
                await self._send_media(chat_id, media, file)
        except TelegramAPIError as e:
            # Не сетевой сбой: повтор не поможет, поэтому не блокируем очередь чата
            log.error("Telegram отклонил сообщение: %s", e)

    async def _fetch(self, media: Media) -> BufferedInputFile | None:
        got = await self.vk.download(media.url, MAX_UPLOAD_BYTES)
        if got is None or not got.data:
            return None
        if got.content_type == "text/html" and not media.filename.endswith((".html", ".htm")):
            return None  # вместо файла пришла страница (например, форма входа)
        return BufferedInputFile(got.data, filename=media.filename or "file")

    async def _call(self, factory: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(5):
            try:
                return await factory()
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (TelegramNetworkError, TelegramServerError) as e:
                if attempt == 4:
                    raise TransientSendError(str(e)) from e
                await asyncio.sleep(2**attempt)
        raise TransientSendError("Telegram: превышен лимит запросов")

    async def _send_text(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if len(text) <= TEXT_LIMIT:
            try:
                await self._call(
                    lambda: self.bot.send_message(
                        chat_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW
                    )
                )
                return
            except TelegramBadRequest as e:
                log.warning("Telegram не принял разметку (%s) — отправляю простым текстом", e)
        # Слишком длинный или не разобранный HTML: режем обычный текст, теги тут уже не сломать
        plain = strip_tags(text)
        chunks = [plain[i : i + TEXT_LIMIT] for i in range(0, len(plain), TEXT_LIMIT)] or ["…"]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            await self._call(
                lambda: self.bot.send_message(
                    chat_id,
                    chunk,
                    parse_mode=None,
                    reply_markup=markup if last else None,
                    link_preview_options=NO_PREVIEW,
                )
            )

    async def _send_album(self, chat_id: int, photos: list[tuple[Media, BufferedInputFile]]) -> None:
        try:
            await self._call(
                lambda: self.bot.send_media_group(
                    chat_id, media=[InputMediaPhoto(media=file) for _, file in photos]
                )
            )
        except TelegramBadRequest:
            for media, file in photos:
                await self._send_media(chat_id, media, file)

    async def _send_media(
        self,
        chat_id: int,
        media: Media,
        file: BufferedInputFile,
        caption: str | None = None,
        markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        kind = media.kind
        if kind == "photo" and len(file.data) > PHOTO_LIMIT:
            kind = "document"
        common = {"caption": caption, "reply_markup": markup}
        try:
            if kind == "photo":
                await self._call(lambda: self.bot.send_photo(chat_id, file, **common))
            elif kind == "voice":
                await self._call(
                    lambda: self.bot.send_voice(chat_id, file, duration=media.duration or None, **common)
                )
            elif kind == "animation":
                await self._call(lambda: self.bot.send_animation(chat_id, file, **common))
            else:
                await self._call(lambda: self.bot.send_document(chat_id, file, **common))
            return
        except TelegramBadRequest as e:
            log.warning("Telegram не принял %s (%s) — отправляю файлом", kind, e)
        # Запасной путь: подпись отдельным сообщением, вложение — как документ
        if caption:
            await self._send_text(chat_id, caption, markup)
        await self._call(lambda: self.bot.send_document(chat_id, file))

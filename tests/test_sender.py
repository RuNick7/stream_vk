"""Что именно уходит в Telegram для разных сообщений."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import (
    SendAnimation,
    SendDocument,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendVoice,
)

from vkstream import sender as sender_module
from vkstream.render import Media, Rendered
from vkstream.sender import TelegramSink, TransientSendError
from vkstream.state import State
from vkstream.vk.client import Downloaded

from .conftest import make_bot

OWNER = 100
URL = "https://vk.ru/im/convo/1?cmid=2"


class StubVK:
    """Отдаёт содержимое по URL; отсутствующий URL — «не скачалось»."""

    def __init__(self, files: dict[str, bytes] | None = None, content_type: str = "image/jpeg"):
        self.files = files or {}
        self.content_type = content_type

    async def download(self, url: str, max_bytes: int) -> Downloaded | None:
        data = self.files.get(url)
        return None if data is None or len(data) > max_bytes else Downloaded(data, self.content_type)


def make_sink(tmp_path: Path, vk: StubVK | None = None, owner: int | None = OWNER):
    bot, session = make_bot()
    state = State(tmp_path / "state.json")
    state.owner_id = owner
    return TelegramSink(bot, state, vk or StubVK()), session


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_delay: float) -> None:
        return None

    monkeypatch.setattr(sender_module.asyncio, "sleep", instant)


def error(kind: type, text: str) -> Exception:
    return kind(method=SendMessage(chat_id=OWNER, text="x"), message=text)


async def test_text_goes_as_html_with_open_button(tmp_path):
    sink, session = make_sink(tmp_path)
    await sink.send(Rendered("<b>Чат</b>\nпривет"), url=URL)
    (msg,) = session.requests
    assert isinstance(msg, SendMessage) and (msg.chat_id, msg.text) == (OWNER, "<b>Чат</b>\nпривет")
    assert msg.link_preview_options.is_disabled
    button = msg.reply_markup.inline_keyboard[0][0]
    assert (button.text, button.url) == ("Открыть в VK", URL)


async def test_nothing_is_sent_until_owner_is_bound(tmp_path):
    sink, session = make_sink(tmp_path, owner=None)
    await sink.send(Rendered("секрет"))
    await sink.notice("служебное")
    assert session.requests == []


async def test_single_photo_carries_text_as_caption(tmp_path):
    sink, session = make_sink(tmp_path, StubVK({"https://cdn/p.jpg": b"jpegdata"}))
    await sink.send(Rendered("<b>Чат</b>\nфото дня", [Media("photo", "https://cdn/p.jpg", "p.jpg")]), url=URL)
    (photo,) = session.requests
    assert isinstance(photo, SendPhoto) and photo.caption == "<b>Чат</b>\nфото дня"
    assert photo.photo.data == b"jpegdata" and photo.reply_markup is not None


async def test_several_photos_become_album_after_text(tmp_path):
    files = {f"https://cdn/{i}.jpg": b"x" for i in range(12)}
    media = [Media("photo", url, "p.jpg") for url in files]
    sink, session = make_sink(tmp_path, StubVK(files))
    await sink.send(Rendered("подборка", media))
    kinds = [type(r) for r in session.requests]
    assert kinds == [SendMessage, SendMediaGroup, SendMediaGroup]
    assert [len(r.media) for r in session.of(SendMediaGroup)] == [10, 2]


async def test_voice_gif_and_document_use_matching_methods(tmp_path):
    files = {"https://cdn/v.ogg": b"v", "https://cdn/c.gif": b"g", "https://cdn/r.pdf": b"d"}
    sink, session = make_sink(tmp_path, StubVK(files, "application/octet-stream"))
    await sink.send(
        Rendered(
            "три вложения",
            [
                Media("voice", "https://cdn/v.ogg", "voice.ogg", duration=7),
                Media("animation", "https://cdn/c.gif", "c.gif"),
                Media("document", "https://cdn/r.pdf", "Отчёт.pdf"),
            ],
        )
    )
    assert [type(r) for r in session.requests] == [SendMessage, SendVoice, SendAnimation, SendDocument]
    assert session.of(SendVoice)[0].duration == 7
    assert session.of(SendDocument)[0].document.filename == "Отчёт.pdf"


async def test_failed_download_turns_into_link(tmp_path):
    sink, session = make_sink(tmp_path)
    await sink.send(Rendered("файл", [Media("document", "https://vk.ru/doc1_2?a=1&b=2", "a.pdf")]))
    (msg,) = session.requests
    assert isinstance(msg, SendMessage)
    assert '📎 <a href="https://vk.ru/doc1_2?a=1&amp;b=2">a.pdf</a> (не удалось загрузить)' in msg.text


async def test_html_page_instead_of_file_is_not_forwarded_as_file(tmp_path):
    sink, session = make_sink(tmp_path, StubVK({"https://vk.ru/doc1_2": b"<html>login</html>"}, "text/html"))
    await sink.send(Rendered("файл", [Media("document", "https://vk.ru/doc1_2", "a.pdf")]))
    assert [type(r) for r in session.requests] == [SendMessage]


async def test_oversized_photo_is_sent_as_document(tmp_path, monkeypatch):
    monkeypatch.setattr(sender_module, "PHOTO_LIMIT", 4)
    sink, session = make_sink(tmp_path, StubVK({"https://cdn/p.jpg": b"12345"}))
    await sink.send(Rendered("большое", [Media("photo", "https://cdn/p.jpg", "p.jpg")]))
    assert [type(r) for r in session.requests] == [SendDocument]


async def test_long_text_is_split_into_plain_chunks(tmp_path):
    sink, session = make_sink(tmp_path)
    await sink.send(Rendered("<b>Чат</b>\n" + "я" * 9000), url=URL)
    parts = session.of(SendMessage)
    assert [len(p.text) for p in parts] == [4096, 4096, 9004 - 8192]
    assert all(p.parse_mode is None for p in parts)
    assert parts[0].text.startswith("Чат\nяя")
    assert [p.reply_markup is not None for p in parts] == [False, False, True]


async def test_rejected_markup_falls_back_to_plain_text(tmp_path):
    sink, session = make_sink(tmp_path)
    session.errors.append(error(TelegramBadRequest, "can't parse entities"))
    await sink.send(Rendered("<b>Чат</b>\n1 &lt; 2"))
    first, second = session.of(SendMessage)
    assert (second.text, second.parse_mode) == ("Чат\n1 < 2", None)


async def test_photo_rejected_by_telegram_is_resent_as_document(tmp_path):
    sink, session = make_sink(tmp_path, StubVK({"https://cdn/p.jpg": b"notanimage"}))
    session.errors.append(error(TelegramBadRequest, "IMAGE_PROCESS_FAILED"))
    await sink.send(Rendered("подпись", [Media("photo", "https://cdn/p.jpg", "p.jpg")]))
    assert [type(r) for r in session.requests] == [SendPhoto, SendMessage, SendDocument]


async def test_network_errors_are_retried_then_reported_as_transient(tmp_path):
    sink, session = make_sink(tmp_path)
    session.errors += [error(TelegramNetworkError, "timeout")] * 2
    await sink.send(Rendered("дойдёт с третьей попытки"))
    assert len(session.requests) == 3

    session.requests.clear()
    session.errors += [error(TelegramNetworkError, "timeout")] * 5
    with pytest.raises(TransientSendError):
        await sink.send(Rendered("не дойдёт"))

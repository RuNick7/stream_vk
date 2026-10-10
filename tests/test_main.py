"""Сборка приложения целиком: поддельный ВК -> пересыльщик -> (записанные) вызовы Telegram."""

from __future__ import annotations

import asyncio
import logging

import pytest
from aiogram.methods import GetMe, SendMessage, SendPhoto, SetMyCommands

import vkstream.__main__ as main_module
from vkstream.state import State, Watch

from .conftest import CHAT, FakeVK, make_bot, wait_until

OWNER = 100


async def test_run_forwards_vk_message_with_photo_to_owner(cfg, fake_vk: FakeVK, monkeypatch, caplog):
    bot, session = make_bot()
    monkeypatch.setattr(main_module, "_make_bot", lambda _cfg: bot)

    state = State(cfg.state_path)
    state.owner_id = OWNER
    state.watches[CHAT] = Watch(CHAT, "Рабочий чат", "chat", 0)
    state.save()
    photo = {"sizes": [{"width": 100, "height": 100, "url": f"{fake_vk.base}/file/photo.jpg"}]}
    fake_vk.add_message(CHAT, "смотрите", attachments=[{"type": "photo", "photo": photo}])
    fake_vk.add_message(CHAT, "и текст")

    caplog.set_level(logging.INFO, logger="vkstream")
    task = asyncio.create_task(main_module.run(cfg, handle_signals=False))
    try:
        await wait_until(lambda: session.of(SendMessage) and session.of(SendPhoto))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert session.of(GetMe) and session.of(SetMyCommands)
    (photo_msg,) = session.of(SendPhoto)
    assert photo_msg.chat_id == OWNER
    assert photo_msg.caption == "💬 <b>Рабочий чат</b>\n👤 <b>Анна Смирнова</b>\nсмотрите"
    (text_msg,) = session.of(SendMessage)
    assert text_msg.text.endswith("и текст")
    assert session.requests.index(photo_msg) < session.requests.index(text_msg)
    assert State.load(cfg.state_path).watches[CHAT].last_cmid == 2
    assert "ещё не привязан" not in caplog.text


async def test_run_prints_pairing_link_when_owner_unknown(cfg, fake_vk: FakeVK, monkeypatch, caplog):
    bot, session = make_bot()
    monkeypatch.setattr(main_module, "_make_bot", lambda _cfg: bot)
    caplog.set_level(logging.INFO, logger="vkstream")
    task = asyncio.create_task(main_module.run(cfg, handle_signals=False))
    try:
        await wait_until(lambda: "https://t.me/test_bot?start=" in caplog.text)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert session.of(SendMessage) == []


def test_run_without_token_explains_what_to_do(cfg):
    with pytest.raises(SystemExit, match="TG_BOT_TOKEN"):
        asyncio.run(main_module.run(cfg))

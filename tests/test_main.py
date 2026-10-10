"""Сборка приложения целиком: поддельный ВК -> пересыльщик -> (записанные) вызовы Telegram."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest
from aiogram.methods import GetMe, SendMessage, SendPhoto, SetMyCommands

import vkstream.__main__ as main_module
from vkstream.state import State

from .conftest import CHAT, SHOP, USER, FakeVK, make_bot, wait_until

OWNER = 100


async def test_run_forwards_by_rules_and_asks_about_group(cfg, fake_vk: FakeVK, monkeypatch, caplog):
    bot, session = make_bot()
    monkeypatch.setattr(main_module, "_make_bot", lambda _cfg: bot)
    cfg = replace(cfg, tg_owner_id=OWNER, poll_interval=30.0)
    fake_vk.add_message(USER, "было раньше")
    fake_vk.add_message(CHAT, "было раньше")
    fake_vk.add_message(SHOP, "было раньше", from_id=SHOP)

    caplog.set_level(logging.INFO, logger="vkstream")
    task = asyncio.create_task(main_module.run(cfg, handle_signals=False))
    try:
        await wait_until(lambda: "прочитан список чатов" in caplog.text and "long poll подключён" in caplog.text)
        photo = {"sizes": [{"width": 100, "height": 100, "url": f"{fake_vk.base}/file/photo.jpg"}]}
        for peer_id, text, extra in (
            (USER, "смотри", {"attachments": [{"type": "photo", "photo": photo}]}),
            (SHOP, "распродажа", {"from_id": SHOP}),
            (CHAT, "всем привет", {}),
        ):
            msg = fake_vk.add_message(peer_id, text, **extra)
            fake_vk.push_event([10004, 1, 0, msg["id"], peer_id, msg["date"], text, {}, {}])
        await wait_until(lambda: session.of(SendPhoto) and len(session.of(SendMessage)) == 2)
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert session.of(GetMe) and session.of(SetMyCommands)
    (photo_msg,) = session.of(SendPhoto)  # личное — сразу, фото с подписью
    assert (photo_msg.chat_id, photo_msg.caption) == (OWNER, "👤 <b>Анна Смирнова</b>\nсмотри")
    group_msg, question = session.of(SendMessage)  # группа — сообщение и вопрос; реклама — ничего
    assert group_msg.text == "👥 <b>Рабочий чат</b>\n👤 <b>Анна Смирнова</b>\nвсем привет"
    assert question.text.startswith("❓ 👥 <b>Рабочий чат</b>")
    assert [(b.text, b.callback_data) for b in question.reply_markup.inline_keyboard[0]] == [
        ("✅ Оставить", f"d:{CHAT}:allow"),
        ("🚫 Заблокировать", f"d:{CHAT}:block"),
    ]
    saved = State.load(cfg.state_path)
    assert saved.chats[CHAT].asked and saved.chats[USER].last_cmid == 2
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


async def test_check_reports_each_capability(cfg, fake_vk: FakeVK, capsys):
    fake_vk.add_message(USER, "привет")
    fake_vk.add_channel()
    fake_vk.add_post("пост")
    await main_module.check(cfg)
    out = capsys.readouterr().out
    assert "[ok] ВК: вошли как Иван Петров (id1)" in out
    assert "[ok] ВК: список чатов читается, диалогов: 1" in out
    assert "[ok] ВК: long poll подключается (режим web)" in out
    assert "[ok] ВК: папка «Запросы» читается" in out
    assert "[ok] ВК: список каналов читается, каналов: 1" in out
    assert "[ok] ВК: посты канала читаются: получено 1, самый свежий №1, ожидался №1" in out
    assert "[!!] Telegram: не задан TG_BOT_TOKEN" in out


async def test_check_names_what_vk_refused(cfg, fake_vk: FakeVK, capsys):
    fake_vk.disabled |= {"channels.get", "messages.getDiff", "messages.getLongPollServer"}
    fake_vk.refused_filters.add("message_request")
    await main_module.check(cfg)
    out = capsys.readouterr().out
    assert "[!!] ВК: long poll недоступен, бот будет работать опросом" in out
    assert "[!!] ВК: папка «Запросы» недоступна" in out
    assert "[!!] ВК: каналы недоступны: [3] channels.get: Unknown method passed" in out


async def test_check_reports_added_walls(cfg, fake_vk: FakeVK, capsys):
    from vkstream.state import ALLOW, CHANNEL, Chat
    from vkstream.vk.wall import wall_key

    fake_vk.add_community()
    fake_vk.add_community(130, "Закрытый клуб", "closed_club")
    fake_vk.communities[130]["closed"] = True
    fake_vk.add_wall_post("запись")
    state = State(cfg.state_path)
    for group_id, title in ((129, "Лекции"), (130, "Закрытый клуб")):
        key = wall_key(-group_id)
        state.chats[key] = Chat(key, title, CHANNEL, api="wall", decision=ALLOW)
    state.save()

    await main_module.check(cfg)
    out = capsys.readouterr().out
    assert "[ok] ВК: стена «Лекции» читается, записей получено: 1" in out
    assert "[!!] ВК: стена «Закрытый клуб» не читается: [15]" in out

"""Пересыльщик целиком против поддельного сервера ВК."""

from __future__ import annotations

import asyncio
import contextlib
import json

from vkstream.forwarder import Forwarder
from vkstream.sender import TransientSendError
from vkstream.state import State, Watch
from vkstream.vk.client import VKClient

from .conftest import CHAT, USER, FakeSink, FakeVK, wait_until, write_session


@contextlib.asynccontextmanager
async def running(cfg, watches: list[Watch] = ()):
    state = State(cfg.state_path)
    for watch in watches:
        state.watches[watch.peer_id] = watch
    vk = VKClient(cfg)
    sink = FakeSink()
    fwd = Forwarder(cfg, state, vk, sink)
    fwd.DEBOUNCE, fwd.AUTH_RETRY, fwd.LP_RETRY = 0.01, 0.02, 0.2
    task = asyncio.create_task(fwd.run())
    try:
        yield fwd, sink
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await vk.close()


def chat_watch(last_cmid: int | None = 0) -> Watch:
    return Watch(CHAT, "Рабочий чат", "chat", last_cmid)


async def test_long_poll_event_forwards_new_message(cfg, fake_vk: FakeVK):
    fake_vk.history[CHAT] = []
    async with running(cfg, [chat_watch()]) as (fwd, sink):
        await wait_until(lambda: fwd.lp_mode == "web")
        fake_vk.calls.clear()
        msg = fake_vk.add_message(CHAT, "всем привет")
        fake_vk.push_event([10004, 1, 0, msg["id"], CHAT, msg["date"], "всем привет", {}, {}])
        await wait_until(lambda: sink.sent)

        assert sink.texts == ["💬 <b>Рабочий чат</b>\n👤 <b>Анна Смирнова</b>\nвсем привет"]
        assert sink.urls == [f"{fake_vk.base}/im/convo/{CHAT}?cmid=1"]
        assert "POST" in fake_vk.lp_requests
        # сработал именно long poll, а не страховочная сверка (она раз в 30 с)
        assert "messages.getConversationsById" not in fake_vk.calls
        saved = json.loads(cfg.state_path.read_text(encoding="utf-8"))
        assert saved["watches"][0]["last_cmid"] == 1


async def test_events_for_other_chats_do_not_cost_requests(cfg, fake_vk: FakeVK):
    fake_vk.history[CHAT] = []
    async with running(cfg, [chat_watch()]) as (fwd, sink):
        await wait_until(lambda: fwd.lp_mode == "web")
        await asyncio.sleep(0.05)
        fake_vk.calls.clear()
        fake_vk.push_event([10004, 1, 0, 555, 777, 1790000000, "чужой чат", {}, {}])
        fake_vk.push_event([63, CHAT, [USER], 1, 1790000000])  # «печатает…» в нашем чате
        await asyncio.sleep(0.3)
        assert fake_vk.calls == [] and sink.sent == []


async def test_falls_back_to_classic_long_poll(cfg, fake_vk: FakeVK):
    fake_vk.disabled.add("messages.getDiff")
    fake_vk.history[CHAT] = []
    async with running(cfg, [chat_watch()]) as (fwd, sink):
        await wait_until(lambda: fwd.lp_mode == "classic")
        msg = fake_vk.add_message(CHAT, "по-старому")
        fake_vk.push_event([4, msg["id"], 1, CHAT, msg["date"], "по-старому"])
        await wait_until(lambda: sink.sent)
        assert "GET" in fake_vk.lp_requests
        assert "по-старому" in sink.texts[0]


async def test_falls_back_to_polling_without_long_poll(cfg, fake_vk: FakeVK):
    fake_vk.disabled |= {"messages.getDiff", "messages.getLongPollServer"}
    fake_vk.history[CHAT] = []
    async with running(cfg, [chat_watch()]) as (fwd, sink):
        await wait_until(lambda: "messages.getLongPollServer" in fake_vk.calls)
        fake_vk.add_message(CHAT, "без long poll")
        await wait_until(lambda: sink.sent)
        assert fwd.lp_mode is None
        assert fake_vk.lp_requests == []
        assert "без long poll" in sink.texts[0]


async def test_catches_up_in_order_and_skips_own_messages(cfg, fake_vk: FakeVK):
    fake_vk.add_message(CHAT, "старое, уже видели")
    fake_vk.add_message(CHAT, "раз")
    fake_vk.add_message(CHAT, "моё", from_id=1, out=1)
    fake_vk.add_message(CHAT, "два")
    async with running(cfg, [chat_watch(last_cmid=1)]) as (fwd, sink):
        await wait_until(lambda: len(sink.sent) == 2)
        await asyncio.sleep(0.1)
        assert [t.split("\n")[-1] for t in sink.texts] == ["раз", "два"]
        assert fwd.state.watches[CHAT].last_cmid == 4


async def test_reports_when_backlog_does_not_fit(cfg, fake_vk: FakeVK):
    for i in range(60):
        fake_vk.add_message(CHAT, f"m{i + 1}")
    async with running(cfg, [chat_watch(last_cmid=0)]) as (fwd, sink):
        await wait_until(lambda: len(sink.sent) == cfg.history_count)
        assert sink.texts[0].endswith("m11") and sink.texts[-1].endswith("m60")
        assert len(sink.notices) == 1 and "более старые пропущены" in sink.notices[0]


async def test_refreshes_token_when_vk_rejects_it(cfg, fake_vk: FakeVK):
    fake_vk.history[CHAT] = []
    async with running(cfg, [chat_watch()]) as (fwd, sink):
        await wait_until(lambda: fwd.lp_mode == "web")
        logins = fake_vk.logins
        fake_vk.expire_token()
        msg = fake_vk.add_message(CHAT, "после смены токена")
        fake_vk.push_event([10004, 1, 0, msg["id"], CHAT, msg["date"], "", {}, {}])
        await wait_until(lambda: sink.sent)
        assert fake_vk.logins == logins + 1
        assert sink.notices == []  # владельца не беспокоим: всё починилось само


async def test_missing_session_asks_to_login_then_recovers(cfg, fake_vk: FakeVK):
    cfg.session_path.unlink()
    fake_vk.add_message(CHAT, "пришло, пока не были подключены")
    async with running(cfg, [chat_watch(last_cmid=0)]) as (fwd, sink):
        await wait_until(lambda: sink.notices)
        assert fwd.auth_ok is False
        assert "python -m vkstream login" in sink.notices[0]
        assert fake_vk.logins == 0  # без сессии в ВК не стучимся

        write_session(cfg)
        await wait_until(lambda: sink.sent)
        assert fwd.auth_ok is True
        assert "✅ ВК подключён: Иван Петров" in sink.notices[1]
        assert "пока не были подключены" in sink.texts[0]


async def test_dead_session_does_not_hammer_login_and_keeps_cookies(cfg, fake_vk: FakeVK):
    fake_vk.session_ok = False
    async with running(cfg, [chat_watch()]) as (fwd, sink):
        await wait_until(lambda: sink.notices)
        await asyncio.sleep(0.2)
        assert fake_vk.logins == 1
        assert len(sink.notices) == 1
    # ответ-отказ с очисткой cookie не должен стереть сохранённую сессию
    assert "good" in cfg.session_path.read_text(encoding="utf-8")


async def test_add_watch_starts_from_now_and_remove_stops(cfg, fake_vk: FakeVK):
    fake_vk.add_message(CHAT, "история до подписки")
    async with running(cfg) as (fwd, sink):
        await wait_until(lambda: fwd.auth_ok)
        watch = await fwd.add_watch(CHAT)
        assert (watch.title, watch.kind, watch.last_cmid) == ("Рабочий чат", "chat", 1)

        await wait_until(lambda: fwd.lp_mode == "web")
        msg = fake_vk.add_message(CHAT, "новое")
        fake_vk.push_event([10004, 1, 0, msg["id"], CHAT, msg["date"], "новое", {}, {}])
        await wait_until(lambda: sink.sent)
        assert len(sink.sent) == 1 and "новое" in sink.texts[0]

        assert fwd.remove_watch(CHAT) is watch
        msg = fake_vk.add_message(CHAT, "уже не нужно")
        fake_vk.push_event([10004, 1, 0, msg["id"], CHAT, msg["date"], "", {}, {}])
        await asyncio.sleep(0.2)
        assert len(sink.sent) == 1
        assert State.load(cfg.state_path).watches == {}


async def test_private_dialog_title_comes_from_profile(cfg, fake_vk: FakeVK):
    fake_vk.add_message(USER, "раньше")
    async with running(cfg) as (fwd, sink):
        await wait_until(lambda: fwd.auth_ok)
        watch = await fwd.add_watch(USER)
        assert (watch.title, watch.kind) == ("Анна Смирнова", "user")
        fake_vk.add_message(USER, "личное")
        fwd.trigger(USER)
        await wait_until(lambda: sink.sent)
        assert sink.texts == ["👤 <b>Анна Смирнова</b>\nличное"]


async def test_telegram_outage_delays_but_does_not_lose_or_duplicate(cfg, fake_vk: FakeVK):
    fake_vk.disabled |= {"messages.getDiff", "messages.getLongPollServer"}  # сверка раз в 0.05 с
    fake_vk.history[CHAT] = []
    async with running(cfg, [chat_watch()]) as (fwd, sink):
        await wait_until(lambda: fwd.auth_ok)
        sink.fail_next = TransientSendError("сеть упала")
        fake_vk.add_message(CHAT, "первое")
        fake_vk.add_message(CHAT, "второе")
        await wait_until(lambda: len(sink.sent) == 2)
        await asyncio.sleep(0.2)
        assert [t.split("\n")[-1] for t in sink.texts] == ["первое", "второе"]

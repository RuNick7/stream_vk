"""Пересыльщик целиком против поддельного сервера ВК."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace

from vkstream.forwarder import CHANNELS, CONVERSATIONS, REQUESTS, Forwarder
from vkstream.sender import TransientSendError
from vkstream.state import ALLOW, ASK, BLOCK, BUSINESS, CHANNEL, GROUP, PENDING, PERSONAL, State
import pytest

from vkstream.vk.client import VKClient, VKError
from vkstream.vk.wall import NotACommunity, wall_key

from .conftest import CHANNEL_ID, CHAT, GROUP_ID, SHOP, USER, FakeSink, FakeVK, wait_until, write_session

STRANGER = 43


@contextlib.asynccontextmanager
async def running(cfg, wait_ready: bool = True):
    """Запустить пересыльщик; состояние читается с диска, как при настоящем старте."""
    state = State.load(cfg.state_path)
    vk = VKClient(cfg)
    sink = FakeSink()
    fwd = Forwarder(cfg, state, vk, sink)
    fwd.DEBOUNCE, fwd.MIN_SWEEP_GAP, fwd.AUTH_RETRY, fwd.LP_RETRY = 0.01, 0.0, 0.02, 0.2
    task = asyncio.create_task(fwd.run())
    try:
        if wait_ready:
            await wait_until(lambda: {CONVERSATIONS, REQUESTS, CHANNELS} <= state.baselined and fwd.lp_mode)
        yield fwd, sink
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await vk.close()


def event(fake: FakeVK, msg: dict) -> None:
    """Событие long poll о новом сообщении, как его шлёт веб-версия."""
    fake.push_event([10004, 1, 0, msg["id"], msg["peer_id"], msg["date"], msg["text"], {}, {}])


def say(fake: FakeVK, peer_id: int, text: str, **kwargs) -> dict:
    msg = fake.add_message(peer_id, text, **kwargs)
    event(fake, msg)
    return msg


def seed(fake: FakeVK) -> None:
    """Переписка, которая была в ВК до запуска бота."""
    fake.add_message(USER, "старое личное")
    fake.add_message(CHAT, "старое в группе")
    fake.add_message(SHOP, "старая реклама", from_id=SHOP)
    fake.add_channel()
    fake.add_post("старый пост")


# --- первое знакомство --------------------------------------------------------


async def test_first_run_learns_chats_but_forwards_no_history(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        await asyncio.sleep(0.1)
        chats = fwd.state.chats
        assert {p: c.kind for p, c in chats.items()} == {
            USER: PERSONAL,
            CHAT: GROUP,
            SHOP: BUSINESS,
            CHANNEL_ID: CHANNEL,
        }
        assert [c.policy for c in (chats[USER], chats[CHAT], chats[SHOP], chats[CHANNEL_ID])] == [
            ALLOW,
            ASK,
            BLOCK,
            ASK,
        ]
        assert chats[CHANNEL_ID].title == "Новости канала" and chats[SHOP].title == "Магазин «Всё по 100»"
        assert sink.sent == [] and sink.asked == [] and sink.notices == []
    assert State.load(cfg.state_path).chats[CHAT].last_cmid == 1


# --- личные: автоматически ----------------------------------------------------


async def test_personal_messages_are_forwarded_without_asking(cfg, fake_vk: FakeVK):
    cfg = replace(cfg, poll_interval=30.0)  # плановая сверка не успеет — сработать должен long poll
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        fake_vk.calls.clear()
        say(fake_vk, USER, "привет!")
        await wait_until(lambda: sink.sent)
        assert sink.texts == ["👤 <b>Анна Смирнова</b>\nпривет!"]
        assert sink.urls == [f"{fake_vk.base}/im/convo/{USER}?cmid=2"]
        assert sink.asked == []
        assert "POST" in fake_vk.lp_requests
        assert fake_vk.calls == ["messages.getConversations", "messages.getHistory"]


async def test_new_dialog_after_start_is_forwarded_from_first_message(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        say(fake_vk, STRANGER, "здравствуйте")
        say(fake_vk, STRANGER, "это Борис")
        await wait_until(lambda: len(sink.sent) == 2)
        assert sink.texts == ["👤 <b>Борис Новиков</b>\nздравствуйте", "👤 <b>Борис Новиков</b>\nэто Борис"]


async def test_message_request_from_non_friend_is_forwarded(cfg, fake_vk: FakeVK):
    cfg = replace(cfg, poll_interval=0.05)  # «Запросы» сверяются только по расписанию
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        fake_vk.requests.add(STRANGER)
        fake_vk.add_message(STRANGER, "добавьте в друзья")
        await wait_until(lambda: sink.sent)
        assert sink.bodies == ["добавьте в друзья"] and fwd.state.chats[STRANGER].kind == PERSONAL


async def test_blocking_one_person_stops_only_them(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    fake_vk.add_message(STRANGER, "раньше")
    async with running(cfg) as (fwd, sink):
        assert fwd.decide(USER, BLOCK).policy == BLOCK
        say(fake_vk, USER, "не дойдёт")
        say(fake_vk, STRANGER, "дойдёт")
        await wait_until(lambda: sink.sent)
        await asyncio.sleep(0.1)
        assert sink.bodies == ["дойдёт"]


# --- бизнес: никогда ----------------------------------------------------------


async def test_business_chats_are_silent_until_explicitly_allowed(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        say(fake_vk, SHOP, "скидки 50%", from_id=SHOP)
        say(fake_vk, -99, "новый магазин?", from_id=-99)  # сообщество, которого раньше не было
        await wait_until(lambda: -99 in fwd.state.chats)
        await asyncio.sleep(0.1)
        assert sink.sent == [] and sink.asked == []
        assert fwd.state.chats[-99].kind == BUSINESS
        assert "messages.getHistory" not in fake_vk.calls  # рекламу даже не читаем

        fwd.decide(SHOP, ALLOW)
        await asyncio.sleep(0.1)
        assert sink.sent == []  # то, что пришло, пока было заблокировано, не пересылается
        say(fake_vk, SHOP, "ваш заказ готов", from_id=SHOP)
        await wait_until(lambda: sink.sent)
        assert sink.texts == ["🏢 <b>Магазин «Всё по 100»</b>\nваш заказ готов"]


# --- группы: по выбору --------------------------------------------------------


async def test_group_first_message_asks_and_holds_the_rest(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        say(fake_vk, CHAT, "первое")
        await wait_until(lambda: sink.asked)
        assert sink.texts == ["👥 <b>Рабочий чат</b>\n👤 <b>Анна Смирнова</b>\nпервое"]
        assert sink.asked == [CHAT] and fwd.state.chats[CHAT].policy == PENDING

        say(fake_vk, CHAT, "второе")
        say(fake_vk, CHAT, "третье")
        await asyncio.sleep(0.2)
        assert len(sink.sent) == 1 and sink.asked == [CHAT]  # ни новых сообщений, ни повторных вопросов

        fwd.decide(CHAT, ALLOW)
        await wait_until(lambda: len(sink.sent) == 3)
        assert sink.bodies == ["первое", "второе", "третье"]

        say(fake_vk, CHAT, "четвёртое")
        await wait_until(lambda: len(sink.sent) == 4)
        assert sink.asked == [CHAT]


async def test_blocked_group_drops_held_messages_and_can_be_reenabled(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        say(fake_vk, CHAT, "первое")
        await wait_until(lambda: sink.asked)
        say(fake_vk, CHAT, "придержанное")
        await asyncio.sleep(0.1)

        fwd.decide(CHAT, BLOCK)
        say(fake_vk, CHAT, "в заблокированной")
        await asyncio.sleep(0.2)
        assert sink.bodies == ["первое"]

        assert fwd.toggle(CHAT).policy == ALLOW  # «передумал» в настройках
        await asyncio.sleep(0.2)
        assert sink.bodies == ["первое"]  # пришедшее во время блокировки не всплывает
        say(fake_vk, CHAT, "после разблокировки")
        await wait_until(lambda: len(sink.sent) == 2)
        assert sink.bodies == ["первое", "после разблокировки"]
        assert sink.asked == [CHAT]


async def test_decision_survives_restart(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        say(fake_vk, CHAT, "первое")
        await wait_until(lambda: sink.asked)
        fwd.decide(CHAT, ALLOW)
    fake_vk.add_message(CHAT, "пока бот был выключен")
    async with running(cfg) as (fwd, sink):
        await wait_until(lambda: sink.sent)
        assert sink.bodies == ["пока бот был выключен"] and sink.asked == []


async def test_own_message_in_undecided_group_does_not_trigger_question(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        say(fake_vk, CHAT, "я сам написал", from_id=1, out=1)
        await wait_until(lambda: fwd.state.chats[CHAT].last_cmid == 2)
        assert sink.sent == [] and sink.asked == []
        say(fake_vk, CHAT, "ответ коллеги")
        await wait_until(lambda: sink.asked)
        assert sink.bodies == ["ответ коллеги"]


async def test_old_style_community_channel_counts_as_channel(cfg, fake_vk: FakeVK):
    fake_vk.old_channels.add(CHAT)
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        assert fwd.state.chats[CHAT].kind == CHANNEL
        say(fake_vk, CHAT, "анонс")
        await wait_until(lambda: sink.asked)
        assert sink.texts == ["📣 <b>Рабочий чат</b>\nанонс"]


# --- каналы -------------------------------------------------------------------


async def test_channel_post_asks_then_follows_decision(cfg, fake_vk: FakeVK):
    cfg = replace(cfg, poll_interval=0.05)  # каналы сверяются только по расписанию
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        fake_vk.add_post("свежий пост")
        await wait_until(lambda: sink.asked)
        assert sink.texts == ["📣 <b>Новости канала</b>\nсвежий пост"]
        assert sink.urls == [None] and sink.asked == [CHANNEL_ID]

        fake_vk.add_post("ещё пост")
        await asyncio.sleep(0.2)
        assert len(sink.sent) == 1

        fwd.decide(CHANNEL_ID, ALLOW)
        photo = {"type": "photo", "photo": {"sizes": [{"width": 9, "height": 9, "url": "https://cdn/p.jpg"}]}}
        fake_vk.add_post("с картинкой", attachments=[photo])
        await wait_until(lambda: len(sink.sent) == 3)
        assert sink.bodies == ["свежий пост", "ещё пост", "с картинкой"]
        assert [m.url for m in sink.sent[2].media] == ["https://cdn/p.jpg"]


async def test_channel_newest_post_arrives_even_if_history_page_omits_it(cfg, fake_vk: FakeVK):
    cfg = replace(cfg, poll_interval=0.05)
    seed(fake_vk)
    fake_vk.history_skips_start = True
    async with running(cfg) as (fwd, sink):
        fwd.decide(CHANNEL_ID, ALLOW)
        fake_vk.add_post("единственный новый")
        await wait_until(lambda: sink.sent)
        assert sink.bodies == ["единственный новый"]
        assert "channels.getById" in fake_vk.calls


async def test_everything_else_works_when_vk_refuses_channels_and_requests(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    fake_vk.disabled.add("channels.get")
    fake_vk.refused_filters.add("message_request")
    async with running(cfg, wait_ready=False) as (fwd, sink):
        await wait_until(lambda: CONVERSATIONS in fwd.state.baselined and fwd.lp_mode)
        await wait_until(lambda: all(fwd.feature_errors.values()))
        assert "Unknown method" in fwd.feature_errors[CHANNELS]
        assert CHANNEL_ID not in fwd.state.chats
        say(fake_vk, USER, "личное всё равно доходит")
        await wait_until(lambda: sink.sent)
        assert sink.notices == []
        calls = len(fake_vk.calls)
        await asyncio.sleep(0.2)
        assert "channels.get" not in fake_vk.calls[calls:]  # недоступное не дёргаем каждую сверку


# --- сигналы и запасные пути --------------------------------------------------


async def test_noise_and_blocked_chats_do_not_cost_requests(cfg, fake_vk: FakeVK):
    cfg = replace(cfg, poll_interval=30.0)
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        fwd.decide(CHAT, BLOCK)
        await asyncio.sleep(0.05)
        fake_vk.calls.clear()
        fake_vk.push_event([63, USER, [USER], 1, 1790000000])  # «печатает…»
        event(fake_vk, fake_vk.add_message(CHAT, "шум в заблокированной группе"))
        event(fake_vk, fake_vk.add_message(SHOP, "реклама", from_id=SHOP))
        await asyncio.sleep(0.3)
        assert fake_vk.calls == [] and sink.sent == []

        fake_vk.push_event([10004, 1, 0, 5, 987654, 1790000000, "кто-то незнакомый", {}, {}])
        await wait_until(lambda: "messages.getConversations" in fake_vk.calls)


async def test_falls_back_to_classic_long_poll(cfg, fake_vk: FakeVK):
    cfg = replace(cfg, poll_interval=30.0)
    fake_vk.disabled.add("messages.getDiff")
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        assert fwd.lp_mode == "classic"
        msg = fake_vk.add_message(USER, "по-старому")
        fake_vk.push_event([4, msg["id"], 1, USER, msg["date"], "по-старому"])
        await wait_until(lambda: sink.sent)
        assert "GET" in fake_vk.lp_requests and sink.bodies == ["по-старому"]


async def test_falls_back_to_polling_without_long_poll(cfg, fake_vk: FakeVK):
    fake_vk.disabled |= {"messages.getDiff", "messages.getLongPollServer"}
    seed(fake_vk)
    async with running(cfg, wait_ready=False) as (fwd, sink):
        await wait_until(lambda: CONVERSATIONS in fwd.state.baselined)
        fake_vk.add_message(USER, "без long poll")
        await wait_until(lambda: sink.sent)
        assert fwd.lp_mode is None and fake_vk.lp_requests == []


# --- догон, сбои, авторизация -------------------------------------------------


async def test_catches_up_after_downtime_in_order_skipping_own(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg):
        pass
    fake_vk.add_message(USER, "раз")
    fake_vk.add_message(USER, "моё", from_id=1, out=1)
    fake_vk.add_message(USER, "два")
    async with running(cfg) as (fwd, sink):
        await wait_until(lambda: len(sink.sent) == 2)
        await asyncio.sleep(0.1)
        assert sink.bodies == ["раз", "два"] and fwd.state.chats[USER].last_cmid == 4


async def test_reports_when_backlog_does_not_fit(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg):
        pass
    for i in range(60):
        fake_vk.add_message(USER, f"m{i + 1}")
    async with running(cfg) as (fwd, sink):
        await wait_until(lambda: len(sink.sent) == cfg.history_count)
        assert sink.bodies[0] == "m11" and sink.bodies[-1] == "m60"
        assert len(sink.notices) == 1 and "более старые пропущены" in sink.notices[0]


async def test_many_active_chats_after_downtime_are_all_found(cfg, fake_vk: FakeVK):
    peers = list(range(1000, 1130))
    for peer_id in peers:
        fake_vk.add_message(peer_id, "до запуска")
    async with running(cfg):
        pass
    for peer_id in peers:
        fake_vk.add_message(peer_id, f"новое от {peer_id}")
    async with running(cfg) as (fwd, sink):
        await wait_until(lambda: len(sink.sent) == len(peers), timeout=10)
        assert sorted(sink.bodies) == sorted(f"новое от {p}" for p in peers)


async def test_chat_vk_no_longer_lets_us_read_is_not_retried_forever(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        fake_vk.history_errors[USER] = 917
        say(fake_vk, USER, "недоступно")
        await wait_until(lambda: fwd.state.chats[USER].last_cmid == 2)
        reads = fake_vk.calls.count("messages.getHistory")
        fake_vk.push_event([10004, 1, 0, 5, 987654, 1790000000, "", {}, {}])
        await asyncio.sleep(0.2)
        assert fake_vk.calls.count("messages.getHistory") == reads == 1
        assert "917" in fwd.read_errors["messages"] and sink.sent == []


async def test_refreshes_token_when_vk_rejects_it(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        logins = fake_vk.logins
        fake_vk.expire_token()
        say(fake_vk, USER, "после смены токена")
        await wait_until(lambda: sink.sent)
        assert fake_vk.logins == logins + 1
        assert sink.notices == []  # владельца не беспокоим: всё починилось само


async def test_missing_session_asks_to_login_then_recovers(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    async with running(cfg):
        pass
    cfg.session_path.unlink()
    fake_vk.add_message(USER, "пришло, пока не были подключены")
    async with running(cfg, wait_ready=False) as (fwd, sink):
        await wait_until(lambda: sink.notices)
        assert fwd.auth_ok is False
        assert "python -m vkstream login" in sink.notices[0]
        assert fake_vk.logins == 1  # без сессии в ВК не стучимся (единственный вход — из первого запуска)

        write_session(cfg)
        await wait_until(lambda: sink.sent)
        assert fwd.auth_ok is True
        assert "✅ ВК подключён: Иван Петров" in sink.notices[1]
        assert sink.bodies == ["пришло, пока не были подключены"]


async def test_dead_session_does_not_hammer_login_and_keeps_cookies(cfg, fake_vk: FakeVK):
    fake_vk.session_ok = False
    async with running(cfg, wait_ready=False) as (fwd, sink):
        await wait_until(lambda: sink.notices)
        await asyncio.sleep(0.2)
        assert fake_vk.logins == 1
        assert len(sink.notices) == 1
    # ответ-отказ с очисткой cookie не должен стереть сохранённую сессию
    assert "good" in cfg.session_path.read_text(encoding="utf-8")


async def test_telegram_outage_delays_but_does_not_lose_or_duplicate(cfg, fake_vk: FakeVK):
    cfg = replace(cfg, poll_interval=0.05)
    seed(fake_vk)
    async with running(cfg) as (fwd, sink):
        sink.fail_next = TransientSendError("сеть упала")
        fake_vk.add_message(USER, "первое")
        fake_vk.add_message(USER, "второе")
        await wait_until(lambda: len(sink.sent) == 2)
        await asyncio.sleep(0.2)
        assert sink.bodies == ["первое", "второе"]


# --- стены сообществ, добавленные по ссылке -----------------------------------


def wall_cfg(cfg):
    return replace(cfg, poll_interval=0.05, wall_poll_interval=0.05)


async def test_added_community_forwards_only_new_wall_posts(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    fake_vk.add_community()
    fake_vk.add_wall_post("старая запись")
    fake_vk.add_wall_post("закреплённая", is_pinned=1)
    async with running(wall_cfg(cfg)) as (fwd, sink):
        chat = await fwd.add_wall(f"https://vk.ru/club{GROUP_ID}")
        assert (chat.title, chat.kind, chat.policy, chat.last_cmid) == ("Лекции", CHANNEL, ALLOW, 2)
        await asyncio.sleep(0.2)
        assert sink.sent == [] and sink.asked == []  # ни старых записей, ни вопросов: вы сами её добавили

        fake_vk.add_wall_post("новая запись")
        await wait_until(lambda: sink.sent)
        await asyncio.sleep(0.2)
        assert sink.texts == ["📣 <b>Лекции</b>\nновая запись"]  # закреплённая не приходит повторно
        assert sink.urls == [f"{fake_vk.base}/wall-{GROUP_ID}_3"]
    assert State.load(cfg.state_path).chats[wall_key(-GROUP_ID)].last_cmid == 3


async def test_wall_is_polled_on_its_own_schedule(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    fake_vk.add_community()
    async with running(replace(cfg, poll_interval=0.02, wall_poll_interval=30.0)) as (fwd, sink):
        await fwd.add_wall("lectures")
        fake_vk.calls.clear()
        fake_vk.add_wall_post("подождёт своей очереди")
        await wait_until(lambda: fake_vk.calls.count("messages.getConversations") >= 5)
        assert "wall.get" not in fake_vk.calls and sink.sent == []


async def test_posts_published_while_bot_was_down_arrive_after_restart(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    fake_vk.add_community()
    async with running(wall_cfg(cfg)) as (fwd, sink):
        await fwd.add_wall(f"club{GROUP_ID}")
    fake_vk.add_wall_post("раз")
    fake_vk.add_wall_post("два")
    async with running(wall_cfg(cfg)) as (fwd, sink):
        await wait_until(lambda: len(sink.sent) == 2)
        assert sink.bodies == ["раз", "два"]


async def test_disabled_wall_is_not_read_and_resumes_from_new_posts(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    fake_vk.add_community()
    async with running(wall_cfg(cfg)) as (fwd, sink):
        chat = await fwd.add_wall(f"club{GROUP_ID}")
        fwd.decide(chat.peer_id, BLOCK)
        fake_vk.calls.clear()
        fake_vk.add_wall_post("пока отключено")
        await asyncio.sleep(0.3)
        assert "wall.get" not in fake_vk.calls and sink.sent == []

        fwd.toggle(chat.peer_id)
        await wait_until(lambda: chat.last_cmid == 1)
        fake_vk.add_wall_post("после включения")
        await wait_until(lambda: sink.sent)
        assert sink.bodies == ["после включения"]


async def test_wall_and_dialog_with_same_community_are_independent(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    fake_vk.add_community()
    fake_vk.add_message(-GROUP_ID, "рассылка от сообщества", from_id=-GROUP_ID)
    async with running(wall_cfg(cfg)) as (fwd, sink):
        wall = await fwd.add_wall(f"club{GROUP_ID}")
        dialog = fwd.state.chats[-GROUP_ID]
        assert (dialog.kind, dialog.policy, wall.policy) == (BUSINESS, BLOCK, ALLOW) and wall.peer_id != dialog.peer_id

        say(fake_vk, -GROUP_ID, "ещё рассылка", from_id=-GROUP_ID)
        fake_vk.add_wall_post("запись на стене")
        await wait_until(lambda: sink.sent)
        await asyncio.sleep(0.2)
        assert sink.bodies == ["запись на стене"]  # сообщения сообщества по-прежнему не пересылаются


async def test_closed_wall_is_refused_on_add(cfg, fake_vk: FakeVK):
    seed(fake_vk)
    fake_vk.add_community()
    fake_vk.communities[GROUP_ID]["closed"] = True
    async with running(cfg) as (fwd, sink):
        with pytest.raises(VKError) as info:
            await fwd.add_wall(f"club{GROUP_ID}")
        assert info.value.code == 15
        with pytest.raises(NotACommunity):
            await fwd.add_wall("https://vk.ru/durov")
        with pytest.raises(NotACommunity):
            await fwd.add_wall("просто текст")
        assert all(chat.api != "wall" for chat in fwd.state.chats.values())

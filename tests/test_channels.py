"""Каналы: недокументированные методы, поэтому разбор обязан быть терпимым."""

from __future__ import annotations

import pytest

from vkstream.state import CHANNEL
from vkstream.vk.channels import channel_history, list_channels, normalize_post
from vkstream.vk.client import VKClient
from vkstream.vk.peers import Names

from .conftest import CHANNEL_ID, FakeVK
from .conftest import _error as error_response


@pytest.fixture
async def vk(cfg):
    client = VKClient(cfg)
    yield client
    await client.close()


def test_post_in_channel_shape_becomes_regular_message():
    post = {"cmid": 12, "author_id": -500, "time": 1790000000, "cm_payload": {"text": "из payload"}}
    assert normalize_post(post, -500) == {
        "conversation_message_id": 12,
        "peer_id": -500,
        "from_id": -500,
        "date": 1790000000,
        "out": 0,
        "text": "из payload",
        "attachments": [],
        "reply_message": None,
        "fwd_messages": [],
    }


def test_post_in_message_shape_is_accepted_too():
    att = [{"type": "photo", "photo": {}}]
    post = {"conversation_message_id": 3, "from_id": 9, "date": 5, "text": "как сообщение", "attachments": att}
    got = normalize_post(post, -500)
    assert (got["conversation_message_id"], got["from_id"], got["date"], got["text"]) == (3, 9, 5, "как сообщение")
    assert got["attachments"] == att


@pytest.mark.parametrize("junk", [None, "строка", [], {}, {"cmid": 0}, {"cmid": "12"}, {"cmid": True}, {"text": "без номера"}])
def test_unrecognised_posts_are_dropped_not_crashing(junk):
    assert normalize_post(junk, -500) is None


async def test_list_channels_reads_title_and_last_post(vk: VKClient, fake_vk: FakeVK):
    fake_vk.add_channel()
    fake_vk.add_channel(-501, "Пустой канал")
    fake_vk.add_post("первый")
    fake_vk.add_post("второй")
    channels = await list_channels(vk, Names())
    assert [(c.peer_id, c.title, c.kind, c.api, c.last_cmid) for c in channels] == [
        (CHANNEL_ID, "Новости канала", CHANNEL, "channels", 2),
        (-501, "Пустой канал", CHANNEL, "channels", 0),
    ]


async def test_list_channels_retries_bare_call_if_vk_dislikes_parameters(vk: VKClient, fake_vk: FakeVK):
    fake_vk.add_channel()
    original, seen = fake_vk.m_channels_get, []

    def picky(form):
        seen.append("count" in form)
        return original(form) if "count" not in form else error_response(100, "invalid parameter: count")

    fake_vk.m_channels_get = picky
    channels = await list_channels(vk, Names())
    assert seen == [True, False] and [c.peer_id for c in channels] == [CHANNEL_ID]


async def test_list_channels_skips_malformed_items(vk: VKClient, fake_vk: FakeVK):
    fake_vk.m_channels_get = lambda form: {
        "items": [
            "мусор",
            {"нет": "канала"},
            {"channel": {"title": "без id"}},
            {"channel": {"channel_id": -9, "title": "Нормальный"}, "last_message": {"cmid": 4, "time": 77}},
        ]
    }
    (channel,) = await list_channels(vk, Names())
    assert (channel.peer_id, channel.last_cmid, channel.last_date) == (-9, 4, 77)


async def test_history_without_known_head_asks_for_newest_post_first(vk: VKClient, fake_vk: FakeVK):
    fake_vk.add_channel()
    for i in range(1, 6):
        fake_vk.add_post(f"пост {i}")
    posts = await channel_history(vk, Names(), CHANNEL_ID, None, 3)
    assert sorted(p["conversation_message_id"] for p in posts) == [3, 4, 5]
    assert fake_vk.calls[-2:] == ["channels.getById", "channels.getHistory"]

    fake_vk.add_channel(-501, "Пустой")
    assert await channel_history(vk, Names(), -501, None, 3) == []

"""Стены сообществ: разбор ссылок, поиск сообщества, приведение записей к сообщениям."""

from __future__ import annotations

import pytest

from vkstream.render import render_message
from vkstream.vk.client import VKClient
from vkstream.vk.peers import Names
from vkstream.vk.wall import (
    Community,
    NotACommunity,
    normalize_wall_post,
    parse_link,
    post_url,
    resolve_community,
    wall_key,
    wall_owner,
    wall_posts,
)

from .conftest import GROUP_ID, FakeVK


@pytest.fixture
async def vk(cfg):
    client = VKClient(cfg)
    yield client
    await client.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://vk.ru/club129312036", "club129312036"),
        ("vk.com/public5", "public5"),
        ("https://m.vk.com/lectures?from=groups", "lectures"),
        ("https://vk.ru/lectures/", "lectures"),
        ("https://vk.ru/wall-129312036_77", "club129312036"),  # ссылка на запись
        ("https://vk.ru/club129312036?w=wall-129312036_5", "club129312036"),
        ("club129312036", "club129312036"),
        ("  @lectures ", "lectures"),
    ],
)
def test_links_to_communities_are_recognised(text, expected):
    assert parse_link(text) == expected


@pytest.mark.parametrize(
    "text",
    ["Рабочий чат", "анна", "lectures", "https://example.com/club1", "https://vk.ru/im/convo/1", "vk.ru/", "vk.ru/a", ""],
)
def test_ordinary_text_is_not_mistaken_for_a_link(text):
    assert parse_link(text) is None  # иначе поиск по названию чата превращался бы в «добавить»


def test_wall_keys_never_collide_with_chat_ids():
    key = wall_key(-GROUP_ID)
    assert wall_owner(key) == -GROUP_ID
    assert key < -10**11  # намного ниже любых id диалогов с сообществами и каналов
    assert post_url("https://vk.ru", -GROUP_ID, 7) == "https://vk.ru/wall-129_7"


async def test_community_is_found_by_id_and_by_name(vk: VKClient, fake_vk: FakeVK):
    fake_vk.add_community()
    assert await resolve_community(vk, f"club{GROUP_ID}") == Community(GROUP_ID, "Лекции")
    assert await resolve_community(vk, f"public{GROUP_ID}") == Community(GROUP_ID, "Лекции")
    assert await resolve_community(vk, "lectures") == Community(GROUP_ID, "Лекции")
    assert "utils.resolveScreenName" not in fake_vk.calls[:2]  # по номеру имя выяснять не нужно


@pytest.mark.parametrize("name", ["durov", "no_such_page", "club999"])
async def test_people_and_missing_pages_are_not_communities(vk: VKClient, fake_vk: FakeVK, name):
    fake_vk.add_community()
    # у человека durov id 1 — и сообщество с тем же номером существует; путать их нельзя
    fake_vk.add_community(1, "Сообщество №1", "club_one")
    with pytest.raises(NotACommunity):
        await resolve_community(vk, name)


async def test_wall_posts_become_messages_with_repost_as_forward(vk: VKClient, fake_vk: FakeVK):
    fake_vk.add_community()
    photo = {"type": "photo", "photo": {"sizes": [{"width": 5, "height": 5, "url": "https://cdn/p.jpg"}]}}
    fake_vk.add_wall_post("расписание", attachments=[photo])
    original = {"from_id": -7, "owner_id": -7, "date": 1_700_000_000, "text": "исходная запись", "attachments": []}
    fake_vk.add_wall_post("смотрите", copy_history=[original])

    names = Names()
    repost, plain = await wall_posts(vk, names, -GROUP_ID, 10)
    assert (plain["conversation_message_id"], plain["text"], plain["out"]) == (1, "расписание", 0)
    assert plain["attachments"] == [photo]

    rendered = render_message(repost, title="Лекции", kind="channel", names=names).text.split("\n")
    assert rendered[:2] == ["📣 <b>Лекции</b>", "смотрите"]
    assert rendered[2].startswith("┃ ↗️ <b>Магазин «Всё по 100»</b>") and rendered[3] == "┃ исходная запись"


@pytest.mark.parametrize("junk", [None, "строка", {}, {"id": 0}, {"id": "5"}, {"text": "без номера"}])
def test_malformed_wall_items_are_dropped(junk):
    assert normalize_wall_post(junk) is None


def test_bare_name_is_accepted_only_when_explicitly_adding():
    assert parse_link("lectures") is None
    assert parse_link("lectures", bare_names=True) == "lectures"
    assert parse_link("Рабочий чат", bare_names=True) is None

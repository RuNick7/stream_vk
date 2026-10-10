"""Стены сообществ: разбор ссылок, поиск сообщества, приведение записей к сообщениям."""

from __future__ import annotations

import pytest

from vkstream.render import render_message
from vkstream.vk.client import VKClient
from vkstream.vk.peers import Names
from vkstream.vk.wall import (
    NotACommunity,
    community_name,
    normalize_wall_post,
    parse_link,
    post_url,
    resolve_community_id,
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


async def test_community_id_comes_from_link_number_or_screen_name(vk: VKClient, fake_vk: FakeVK):
    fake_vk.add_community()
    assert await resolve_community_id(vk, f"club{GROUP_ID}") == GROUP_ID
    assert await resolve_community_id(vk, f"public{GROUP_ID}") == GROUP_ID
    assert fake_vk.calls == []  # номер уже есть в ссылке — в ВК ходить незачем
    assert await resolve_community_id(vk, "lectures") == GROUP_ID


@pytest.mark.parametrize(("name", "detail"), [("durov", "это user"), ("no_such_page", "такой страницы нет")])
async def test_people_and_missing_pages_are_not_communities(vk: VKClient, fake_vk: FakeVK, name, detail):
    fake_vk.add_community()
    # у человека durov id 1 — и сообщество с тем же номером существует; путать их нельзя
    fake_vk.add_community(1, "Сообщество №1", "club_one")
    with pytest.raises(NotACommunity) as info:
        await resolve_community_id(vk, name)
    assert info.value.detail == detail


async def test_community_name_comes_with_the_wall(vk: VKClient, fake_vk: FakeVK):
    fake_vk.add_community()
    names = Names()
    await wall_posts(vk, names, -GROUP_ID, 5)
    fake_vk.calls.clear()
    assert await community_name(vk, names, GROUP_ID) == "Лекции"
    assert fake_vk.calls == []  # отдельный запрос за названием не понадобился


async def test_community_name_falls_back_to_lookup_then_to_placeholder(vk: VKClient, fake_vk: FakeVK):
    fake_vk.add_community()
    fake_vk.wall_names_owner = False
    names = Names()
    await wall_posts(vk, names, -GROUP_ID, 5)
    assert await community_name(vk, names, GROUP_ID) == "Лекции"
    assert fake_vk.calls[-1] == "groups.getById"

    fake_vk.disabled.add("groups.getById")  # название — не повод отказывать в подписке
    assert await community_name(vk, Names(), GROUP_ID) == f"Сообщество {GROUP_ID}"


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

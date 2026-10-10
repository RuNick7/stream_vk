"""Стены сообществ: подписка по ссылке и чтение новых записей.

Стена — не чат мессенджера: сигналов о новых записях ВК не шлёт, поэтому она
опрашивается по расписанию. Запись стены приводится к виду обычного сообщения,
чтобы её оформлял тот же код, что и сообщения из чатов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .client import VKClient, VKError
from .peers import Names

# Ключи записей о стенах в State.chats. У стены сообщества тот же id, что и у
# диалога с этим сообществом (-id), поэтому её ключ сдвинут в диапазон, где
# идентификаторов чатов не бывает.
WALL_KEY_OFFSET = -1_000_000_000_000

_HOST = r"^(?:https?://)?(?:m\.)?vk\.(?:ru|com)/"
_LINK = re.compile(_HOST + r"([A-Za-z0-9_.]+)/?(?:[?#].*)?$")
_POST_LINK = re.compile(_HOST + r"wall-(\d+)_\d+")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{1,}$")
_NUMERIC = re.compile(r"^(?:club|public|event)(\d+)$")
COMMUNITY_TYPES = ("group", "page", "event")


def wall_key(owner_id: int) -> int:
    return WALL_KEY_OFFSET + owner_id


def wall_owner(key: int) -> int:
    return key - WALL_KEY_OFFSET


def post_url(origin: str, owner_id: int, post_id: int) -> str:
    return f"{origin}/wall{owner_id}_{post_id}"


def parse_link(text: str, bare_names: bool = False) -> str | None:
    """Короткое имя страницы из ссылки вида vk.ru/club1 или vk.com/name; None — это не ссылка ВК.

    Просто слово (`lectures`) считается именем страницы только с bare_names: в
    обычном сообщении боту это поисковый запрос, а не просьба что-то добавить.
    """
    text = text.strip()
    post = _POST_LINK.match(text)
    if post:  # ссылка на отдельную запись — берём её сообщество
        return f"club{post.group(1)}"
    match = _LINK.match(text)
    name = match.group(1) if match else text.removeprefix("@")
    if not (match or bare_names or text.startswith("@") or _NUMERIC.match(name)):
        return None
    return name if _NAME.match(name) else None


@dataclass(frozen=True)
class Community:
    id: int  # положительный id сообщества; владелец стены — минус этот id
    name: str


class NotACommunity(Exception):
    """Ссылка ведёт не на сообщество (на человека, приложение) или никуда."""


async def resolve_community(vk: VKClient, screen_name: str) -> Community:
    numeric = _NUMERIC.match(screen_name)
    if numeric:
        group_id = int(numeric.group(1))
    else:
        resolved = await vk.call("utils.resolveScreenName", screen_name=screen_name)
        if not isinstance(resolved, dict) or resolved.get("type") not in COMMUNITY_TYPES:
            raise NotACommunity(screen_name)
        group_id = int(resolved["object_id"])
    try:
        resp = await vk.call("groups.getById", group_id=group_id)
    except VKError as e:
        if e.code == 100:  # «неверный параметр» — такого сообщества нет
            raise NotACommunity(screen_name) from None
        raise
    # до версии API 5.139 метод отдавал просто список сообществ
    groups = resp.get("groups") if isinstance(resp, dict) else resp
    if not groups or not groups[0].get("id") or groups[0].get("deactivated"):
        raise NotACommunity(screen_name)
    return Community(int(groups[0]["id"]), groups[0].get("name") or screen_name)


async def wall_posts(vk: VKClient, names: Names, owner_id: int, count: int) -> list[dict]:
    """Последние записи стены в виде сообщений (закреплённая может быть старой)."""
    resp = await vk.call("wall.get", owner_id=owner_id, count=count, extended=1) or {}
    names.update(resp)
    return [post for post in map(normalize_wall_post, resp.get("items") or []) if post]


def normalize_wall_post(item: object) -> dict | None:
    """Запись стены -> словарь в формате сообщения messages.getHistory."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), int) or item["id"] <= 0:
        return None
    return {
        # номер записи растёт вместе со стеной — он и служит курсором
        "conversation_message_id": item["id"],
        "peer_id": item.get("owner_id"),
        "from_id": item.get("from_id") or item.get("owner_id"),
        "date": item.get("date") or 0,
        "out": 0,
        "text": item.get("text") or "",
        "attachments": item.get("attachments") or [],
        # репост показываем как пересланное сообщение от исходного автора
        "fwd_messages": [
            {
                "from_id": original.get("from_id") or original.get("owner_id"),
                "date": original.get("date") or 0,
                "text": original.get("text") or "",
                "attachments": original.get("attachments") or [],
            }
            for original in item.get("copy_history") or []
            if isinstance(original, dict)
        ],
    }

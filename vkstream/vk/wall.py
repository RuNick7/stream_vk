"""Стены сообществ: подписка по ссылке и чтение новых записей.

Стена — не чат мессенджера: сигналов о новых записях ВК не шлёт, поэтому она
опрашивается по расписанию. Запись стены приводится к виду обычного сообщения,
чтобы её оформлял тот же код, что и сообщения из чатов.
"""

from __future__ import annotations

import re
from .client import VKAuthError, VKClient, VKError
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


class NotACommunity(Exception):
    """Ссылка ведёт не на сообщество (на человека, приложение) или никуда."""

    def __init__(self, link: str, detail: str = ""):
        super().__init__(link)
        self.detail = detail  # что именно ответил ВК — для сообщения пользователю


async def resolve_community_id(vk: VKClient, screen_name: str) -> int:
    """Положительный id сообщества по короткому имени (владелец стены — минус этот id)."""
    numeric = _NUMERIC.match(screen_name)
    if numeric:
        return int(numeric.group(1))
    resolved = await vk.call("utils.resolveScreenName", screen_name=screen_name)
    kind = resolved.get("type") if isinstance(resolved, dict) else None
    if kind not in COMMUNITY_TYPES or not resolved.get("object_id"):
        raise NotACommunity(screen_name, f"это {kind}" if kind else "такой страницы нет")
    return int(resolved["object_id"])


async def community_name(vk: VKClient, names: Names, group_id: int) -> str:
    """Название сообщества. Обычно оно уже пришло вместе со стеной (extended=1)."""
    known = names.find(-group_id)
    if known:
        return known
    try:
        # именно group_ids: так метод вызывает сам vk.ru
        resp = await vk.call("groups.getById", group_ids=group_id)
        groups = resp.get("groups") if isinstance(resp, dict) else resp
        return str(groups[0]["name"])
    except VKAuthError:
        raise
    except (VKError, LookupError, TypeError, AttributeError):
        return f"Сообщество {group_id}"  # без названия жить можно, без стены — нет


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

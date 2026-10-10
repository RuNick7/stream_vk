"""Каналы мессенджера ВК.

Каналы — отдельная сущность: в messages.getConversations их нет, читаются они
своими методами веб-клиента (channels.*). Методы не документированы, формат
ответов известен по открытым клиентам, поэтому разбор здесь терпим к вариантам
названий полей, а любая ошибка означает лишь «каналы сейчас недоступны».
"""

from __future__ import annotations

from typing import Any

from ..state import CHANNEL
from .client import VKClient, VKError
from .peers import Names, PeerInfo


def _first_int(obj: dict, *keys: str) -> int | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _post_cmid(post: dict) -> int | None:
    return _first_int(post, "cmid", "conversation_message_id", "id")


async def list_channels(vk: VKClient, names: Names) -> list[PeerInfo]:
    """Каналы, на которые подписан пользователь, с номером последнего поста."""
    try:
        resp = await vk.call("channels.get", count=100, extended=1)
    except VKError as e:
        if e.code != 100:  # 100 — не понравился параметр; пробуем как веб-клиент, без них
            raise
        resp = await vk.call("channels.get")
    resp = resp or {}
    names.update(resp)
    result = []
    for item in resp.get("items") or []:
        channel = item.get("channel") if isinstance(item, dict) else None
        if not isinstance(channel, dict):
            continue
        channel_id = _first_int(channel, "channel_id", "id")
        if channel_id is None:
            continue
        last = item.get("last_message") if isinstance(item.get("last_message"), dict) else None
        read_state = (channel.get("user_data") or {}).get("read_state") or {}
        result.append(
            PeerInfo(
                peer_id=channel_id,
                title=channel.get("title") or "Канал",
                kind=CHANNEL,
                # у канала без постов last_message нет — это честный ноль
                last_cmid=_post_cmid(last) if last else 0,
                last_date=_first_int(last, "time", "date") or 0 if last else 0,
                unread=int(read_state.get("unread_count") or 0),
                api="channels",
            )
        )
    return result


async def channel_history(
    vk: VKClient, names: Names, channel_id: int, head_cmid: int | None, count: int
) -> list[dict]:
    """Последние посты канала в виде обычных сообщений."""
    newest = None
    if not head_cmid:
        newest = await _newest_post(vk, channel_id)
        if newest is None:
            return []
        head_cmid = newest["conversation_message_id"]
    resp = await vk.call(
        "channels.getHistory", channel_id=channel_id, start_cmid=head_cmid, count=count, offset=-1, extended=1
    )
    resp = resp or {}
    names.update(resp)
    posts = [post for post in (normalize_post(item, channel_id) for item in resp.get("items") or []) if post]
    if all(post["conversation_message_id"] != head_cmid for post in posts):
        # Как именно метод отсчитывает страницу от start_cmid, достоверно неизвестно,
        # поэтому самый свежий пост при необходимости добираем отдельно.
        newest = newest or await _newest_post(vk, channel_id)
        if newest is not None and all(p["conversation_message_id"] != newest["conversation_message_id"] for p in posts):
            posts.append(newest)
    return posts


async def _newest_post(vk: VKClient, channel_id: int) -> dict | None:
    resp = await vk.call("channels.getById", channel_ids=channel_id, extended=1) or {}
    for item in resp.get("items") or []:
        if isinstance(item, dict):
            return normalize_post(item.get("last_message"), channel_id)
    return None


def normalize_post(item: Any, channel_id: int) -> dict | None:
    """Пост канала -> словарь в формате сообщения messages.getHistory."""
    if not isinstance(item, dict):
        return None
    cmid = _post_cmid(item)
    if not cmid or cmid < 0:
        return None
    payload = item.get("cm_payload") if isinstance(item.get("cm_payload"), dict) else {}
    return {
        "conversation_message_id": cmid,
        "peer_id": channel_id,
        "from_id": _first_int(item, "from_id", "author_id") or channel_id,
        "date": _first_int(item, "date", "time") or 0,
        "out": 0,
        "text": item.get("text") or payload.get("text") or "",
        "attachments": item.get("attachments") or payload.get("attachments") or [],
        "reply_message": item.get("reply_message"),
        "fwd_messages": item.get("fwd_messages") or [],
    }

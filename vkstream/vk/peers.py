"""Чаты ВК (peer'ы) и имена их участников."""

from __future__ import annotations

from dataclasses import dataclass

from .client import VKClient

CHAT_PEER_OFFSET = 2_000_000_000


class Names:
    """Кэш имён: id > 0 — пользователь, id < 0 — сообщество."""

    def __init__(self) -> None:
        self._names: dict[int, str] = {}

    def update(self, payload: dict | None) -> None:
        """Запомнить profiles/groups из extended-ответа API."""
        if not payload:
            return
        for p in payload.get("profiles") or []:
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            if name:
                self._names[p["id"]] = name
        for g in payload.get("groups") or []:
            if g.get("name"):
                self._names[-g["id"]] = g["name"]

    def get(self, peer_id: int | None) -> str:
        if not peer_id:
            return "?"
        return self._names.get(peer_id) or (f"id{peer_id}" if peer_id > 0 else f"club{-peer_id}")


@dataclass(frozen=True)
class PeerInfo:
    peer_id: int
    title: str
    kind: str  # user | chat | group | email
    last_cmid: int | None


def peer_info(conv: dict, names: Names) -> PeerInfo:
    peer = conv.get("peer") or {}
    peer_id = peer["id"]
    kind = peer.get("type") or ("chat" if peer_id >= CHAT_PEER_OFFSET else "user" if peer_id > 0 else "group")
    if kind == "chat":
        title = (conv.get("chat_settings") or {}).get("title") or f"Беседа {peer_id - CHAT_PEER_OFFSET}"
    else:
        title = names.get(peer_id)
    return PeerInfo(peer_id, title, kind, conv.get("last_conversation_message_id"))


async def list_conversations(
    vk: VKClient, names: Names, offset: int, count: int
) -> tuple[list[PeerInfo], int]:
    """Страница списка чатов в том же порядке, что и в мессенджере. -> (чаты, всего)."""
    resp = await vk.call("messages.getConversations", offset=offset, count=count, extended=1) or {}
    names.update(resp)
    items = [peer_info(item["conversation"], names) for item in resp.get("items") or []]
    return items, int(resp.get("count") or len(items))


async def search_conversations(vk: VKClient, names: Names, query: str, count: int) -> list[PeerInfo]:
    resp = await vk.call("messages.searchConversations", q=query, count=count, extended=1) or {}
    names.update(resp)
    return [peer_info(conv, names) for conv in resp.get("items") or []]


async def get_conversation(vk: VKClient, names: Names, peer_id: int) -> PeerInfo | None:
    resp = await vk.call("messages.getConversationsById", peer_ids=peer_id, extended=1) or {}
    names.update(resp)
    for conv in resp.get("items") or []:
        info = peer_info(conv, names)
        if info.peer_id == peer_id:
            return info
    return None

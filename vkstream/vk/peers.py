"""Чаты ВК (peer'ы) и имена их участников."""

from __future__ import annotations

from dataclasses import dataclass

from ..state import BUSINESS, CHANNEL, GROUP, PERSONAL
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

    def find(self, peer_id: int) -> str | None:
        """Имя, если оно известно (get подставил бы заглушку вида club123)."""
        return self._names.get(peer_id)

    def get(self, peer_id: int | None) -> str:
        if not peer_id:
            return "?"
        return self._names.get(peer_id) or (f"id{peer_id}" if peer_id > 0 else f"club{-peer_id}")


@dataclass(frozen=True)
class PeerInfo:
    peer_id: int
    title: str
    kind: str  # personal | group | channel | business
    last_cmid: int | None
    last_date: int = 0
    unread: int = 0
    api: str = "messages"


def peer_info(conv: dict, names: Names, last_message: dict | None = None) -> PeerInfo:
    peer = conv.get("peer") or {}
    peer_id = peer["id"]
    vk_type = peer.get("type") or ("chat" if peer_id >= CHAT_PEER_OFFSET else "user" if peer_id > 0 else "group")
    settings = conv.get("chat_settings") or {}
    if vk_type == "chat":
        # «Канал сообщества» старого образца — беседа, где пишут только администраторы
        kind = CHANNEL if settings.get("is_group_channel") else GROUP
        title = settings.get("title") or f"Беседа {peer_id - CHAT_PEER_OFFSET}"
    else:
        # Диалог с сообществом (и редкая переписка по e-mail) — «бизнес»; остальное — люди
        kind = BUSINESS if vk_type in ("group", "email") else PERSONAL
        title = names.get(peer_id)
    return PeerInfo(
        peer_id,
        title,
        kind,
        conv.get("last_conversation_message_id"),
        int((last_message or {}).get("date") or 0),
        int(conv.get("unread_count") or 0),
    )


async def list_conversations(
    vk: VKClient, names: Names, offset: int, count: int, filter: str | None = None
) -> tuple[list[PeerInfo], int]:
    """Страница списка чатов в том же порядке, что и в мессенджере. -> (чаты, всего)."""
    resp = await vk.call("messages.getConversations", offset=offset, count=count, extended=1, filter=filter) or {}
    names.update(resp)
    items = [
        peer_info(item["conversation"], names, item.get("last_message")) for item in resp.get("items") or []
    ]
    return items, int(resp.get("count") or len(items))

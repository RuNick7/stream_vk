"""Состояние бота на диске: владелец, известные чаты ВК и решения по ним."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

# Категории чатов — те же, что вкладки мессенджера ВК
PERSONAL, GROUP, CHANNEL, BUSINESS = "personal", "group", "channel", "business"
KINDS = (PERSONAL, GROUP, CHANNEL, BUSINESS)

ALLOW, BLOCK, ASK, PENDING = "allow", "block", "ask", "pending"

# Правило категории, пока по конкретному чату нет решения владельца
DEFAULT_POLICY = {PERSONAL: ALLOW, GROUP: ASK, CHANNEL: ASK, BUSINESS: BLOCK}


def write_private_json(path: Path, data: Any) -> None:
    """Атомарно записать JSON с правами 0600 (в файлах лежат сессия и переписка)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


@dataclass
class Chat:
    peer_id: int
    title: str
    kind: str  # personal | group | channel | business
    # Каким API читается история: messages (диалоги и беседы) или channels (каналы)
    api: str = "messages"
    # conversation_message_id последнего обработанного сообщения; None — курсор
    # не поставлен: при первом чтении встанет на последнее сообщение чата
    last_cmid: int | None = None
    # ...и последнего сообщения, которое есть в ВК (по списку чатов)
    head_cmid: int | None = None
    decision: str | None = None  # allow | block; None — действует правило категории
    asked: bool = False  # вопрос «пересылать?» задан и ждёт ответа
    last_date: int = 0  # время последнего сообщения — для сортировки в настройках

    @property
    def policy(self) -> str:
        """allow | block | ask (нужно спросить) | pending (спросили, ждём ответа)."""
        if self.decision:
            return self.decision
        default = DEFAULT_POLICY.get(self.kind, ASK)
        return PENDING if default == ASK and self.asked else default

    @property
    def has_news(self) -> bool:
        """В ВК есть сообщения новее курсора (или курсор ещё не поставлен)."""
        if self.head_cmid is None:
            return False
        return self.last_cmid is None or self.head_cmid > self.last_cmid


_CHAT_FIELDS = {f.name for f in fields(Chat)}


class State:
    def __init__(self, path: Path):
        self.path = path
        self.owner_id: int | None = None
        self.chats: dict[int, Chat] = {}
        # Какие списки ВК уже прочитаны целиком хотя бы раз. Чат, появившийся
        # в списке позже, считается новым, и его сообщения пересылаются.
        self.baselined: set[str] = set()

    @classmethod
    def load(cls, path: Path) -> State:
        state = cls(path)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            state.owner_id = raw.get("owner_id")
            state.baselined = set(raw.get("baselined", []))
            for item in raw.get("chats", []):
                chat = Chat(**{k: v for k, v in item.items() if k in _CHAT_FIELDS})
                state.chats[chat.peer_id] = chat
        return state

    def save(self) -> None:
        write_private_json(
            self.path,
            {
                "owner_id": self.owner_id,
                "baselined": sorted(self.baselined),
                "chats": [asdict(c) for c in self.chats.values()],
            },
        )

    def of_kind(self, kind: str) -> list[Chat]:
        """Чаты категории, сначала с самыми свежими сообщениями."""
        chats = [c for c in self.chats.values() if c.kind == kind]
        return sorted(chats, key=lambda c: (-c.last_date, c.title.casefold()))

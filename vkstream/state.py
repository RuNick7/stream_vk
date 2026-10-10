"""Состояние бота на диске: владелец и выбранные чаты ВК."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def write_private_json(path: Path, data: Any) -> None:
    """Атомарно записать JSON с правами 0600 (в файлах лежат сессия и переписка)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


@dataclass
class Watch:
    peer_id: int
    title: str
    kind: str  # user | chat | group | email
    # conversation_message_id последнего обработанного сообщения;
    # None — ещё не знаем, с какого места начинать
    last_cmid: int | None = None


class State:
    def __init__(self, path: Path):
        self.path = path
        self.owner_id: int | None = None
        self.watches: dict[int, Watch] = {}

    @classmethod
    def load(cls, path: Path) -> State:
        state = cls(path)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            state.owner_id = raw.get("owner_id")
            for item in raw.get("watches", []):
                watch = Watch(**item)
                state.watches[watch.peer_id] = watch
        return state

    def save(self) -> None:
        write_private_json(
            self.path,
            {
                "owner_id": self.owner_id,
                "watches": [asdict(w) for w in self.watches.values()],
            },
        )

"""Long poll мессенджера VK.

События используются только как сигнал «в этом чате что-то произошло»: сами
сообщения потом забираются через messages.getHistory. Так не нужно разбирать
формат событий, который у веб-клиента не документирован и меняется.
"""

from __future__ import annotations

from typing import Any, Collection

from .client import VKAuthError, VKClient, VKError

# Коды событий, после которых новых сообщений не бывает: флаги, прочтения,
# онлайн, «печатает…», звонки, счётчики. Веб-версия шлёт часть кодов со
# сдвигом на 10000 (10004 — новое сообщение), поэтому сравнение по модулю.
NOISE_CODES = frozenset(
    {1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 80, 81, 114}
)


class LongPollExpired(Exception):
    """Ключ или ts устарели — нужно переподключиться."""


class LongPollUnavailable(Exception):
    """Ни один из способов получить long poll сервер не сработал."""


class LongPoll:
    WAIT = 25

    def __init__(self, vk: VKClient):
        self.vk = vk
        self.mode: str | None = None  # web | classic
        self._server = ""
        self._key = ""
        self._ts = 0

    @property
    def connected(self) -> bool:
        return self.mode is not None

    def reset(self) -> None:
        self.mode = None

    async def connect(self) -> None:
        errors = []
        for mode, connect in (("web", self._connect_web), ("classic", self._connect_classic)):
            try:
                await connect()
            except VKAuthError:
                raise
            except (VKError, KeyError, TypeError, ValueError) as e:
                errors.append(f"{mode}: {e!r}")
                continue
            self.mode = mode
            return
        raise LongPollUnavailable("; ".join(errors))

    async def _connect_web(self) -> None:
        """Как vk.ru в браузере: messages.getDiff -> credentials -> api.vk.ru/ruim<id>."""
        resp = await self.vk.call(
            "messages.getDiff",
            lp_version=21,
            conversations_limit=0,
            extended_filters="credentials,server_version",
            group_id=0,
        )
        creds = resp["credentials"]
        self._set(creds["server_lp"], creds["key"], creds["ts"])

    async def _connect_classic(self) -> None:
        resp = await self.vk.call("messages.getLongPollServer", lp_version=3)
        self._set(resp["server"], resp["key"], resp["ts"])

    def _set(self, server: Any, key: Any, ts: Any) -> None:
        server = str(server)
        if not server.startswith(("http://", "https://")):
            server = "https://" + server
        self._server, self._key, self._ts = server, str(key), int(ts)

    async def poll(self) -> list:
        check = {"act": "a_check", "key": self._key, "ts": str(self._ts), "wait": str(self.WAIT)}
        timeout = self.WAIT + 15
        if self.mode == "web":
            data = await self.vk.request_json(
                "POST", self._server, params={"version": "21", "mode": "682"}, data=check, timeout=timeout
            )
        else:
            data = await self.vk.request_json(
                "GET", self._server, params={**check, "mode": "2", "version": "3"}, timeout=timeout
            )
        if not isinstance(data, dict):
            return []
        failed = data.get("failed")
        if failed == 1 and "ts" in data:
            self._ts = int(data["ts"])
            return []
        if failed:
            self.reset()
            raise LongPollExpired(f"failed={failed}")
        if "ts" in data:
            self._ts = int(data["ts"])
        updates = data.get("updates")
        return updates if isinstance(updates, list) else []


def is_noise(update: Any) -> bool:
    """Событие, после которого новых сообщений точно нет (или вовсе не событие)."""
    if not isinstance(update, list) or not update:
        return True
    code = update[0]
    return isinstance(code, int) and code % 10000 in NOISE_CODES


def peers_in_update(update: Any, known: Collection[int]) -> set[int]:
    """Какие из известных чатов упомянуты в событии long poll."""
    if is_noise(update):
        return set()
    found: set[int] = set()
    stack: list[Any] = list(update[1:])
    while stack:
        item = stack.pop()
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            if item in known:
                found.add(item)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
    return found

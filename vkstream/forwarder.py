"""Слежение за выбранными чатами ВК и пересылка новых сообщений.

Три источника сигнала «в чате что-то новое» сходятся в одну очередь:
long poll (мгновенно), периодическая сверка (страховка, она же — догон после
простоя) и ручное добавление чата. Сами сообщения всегда читаются через
messages.getHistory по курсору conversation_message_id, поэтому повторный или
лишний сигнал безвреден, а пропущенный — навёрстывается сверкой.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

import aiohttp

from .config import Config
from .render import Rendered, esc, message_url, render_message
from .sender import TransientSendError
from .state import State, Watch
from .vk.client import VKAuthError, VKClient, VKError
from .vk.longpoll import LongPoll, LongPollExpired, LongPollUnavailable, peers_in_update
from .vk.peers import Names, get_conversation, peer_info

log = logging.getLogger(__name__)

NETWORK_ERRORS = (VKError, aiohttp.ClientError, asyncio.TimeoutError, OSError)

LOGIN_HINT = "Выполните на компьютере, где запущен бот:\n<code>python -m vkstream login</code>"


class Sink(Protocol):
    async def send(self, rendered: Rendered, url: str | None = None) -> None: ...
    async def notice(self, text: str) -> None: ...


class Forwarder:
    DEBOUNCE = 0.3  # сек: склеиваем пачку событий по одному чату в один запрос
    AUTH_RETRY = 3.0  # как часто проверять, не появилась ли новая сессия
    AUTH_RECHECK = 900.0  # как часто перепроверять старую сессию без изменений на диске
    LP_RETRY = 300.0  # когда снова пробовать long poll, если он недоступен

    def __init__(self, cfg: Config, state: State, vk: VKClient, sink: Sink):
        self.cfg = cfg
        self.state = state
        self.vk = vk
        self.sink = sink
        self.names = Names()
        self.auth_ok: bool | None = None  # None — ещё подключаемся
        self.auth_error = ""
        self.lp_mode: str | None = None
        self.forwarded = 0
        self.last_forward_at: float | None = None
        self._authed = asyncio.Event()
        self._need_auth = asyncio.Event()
        self._need_auth.set()
        self._pending: set[int] = set()
        self._pending_evt = asyncio.Event()
        self._sweep_now = asyncio.Event()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._auth_loop())
            tg.create_task(self._longpoll_loop())
            tg.create_task(self._sweep_loop())
            tg.create_task(self._worker())

    # --- управление списком чатов (вызывается из бота) -------------------------

    async def add_watch(self, peer_id: int) -> Watch:
        info = await get_conversation(self.vk, self.names, peer_id)
        if info is None:
            raise VKError(-1, "чат не найден")
        # Курсор ставим на последнее сообщение: пересылаем только новые
        watch = Watch(info.peer_id, info.title, info.kind, info.last_cmid)
        self.state.watches[peer_id] = watch
        self.state.save()
        self._sweep_now.set()
        return watch

    def remove_watch(self, peer_id: int) -> Watch | None:
        watch = self.state.watches.pop(peer_id, None)
        if watch is not None:
            self.state.save()
        return watch

    def trigger(self, peer_id: int) -> None:
        self._pending.add(peer_id)
        self._pending_evt.set()

    # --- авторизация -----------------------------------------------------------

    def _auth_failed(self, exc: Exception) -> None:
        if self._authed.is_set():
            log.warning("VK: авторизация потеряна: %s", exc)
            self._authed.clear()
            self._need_auth.set()

    async def _auth_loop(self) -> None:
        while True:
            await self._need_auth.wait()
            try:
                await self.vk.authorize()
            except VKAuthError as e:
                if self.auth_ok is not False:
                    self.auth_ok, self.auth_error = False, e.message
                    log.warning("VK не подключён: %s", e.message)
                    await self._notify(f"⚠️ ВК не подключён: {esc(e.message)}.\n{LOGIN_HINT}")
                await self._wait_new_session()
                continue
            except NETWORK_ERRORS as e:
                log.warning("VK недоступен: %s", e)
                await asyncio.sleep(self.AUTH_RETRY)
                continue
            except Exception:
                log.exception("VK: сбой при входе")
                await asyncio.sleep(self.AUTH_RETRY)
                continue
            recovered = self.auth_ok is False
            self.auth_ok, self.auth_error = True, ""
            self._need_auth.clear()
            self._authed.set()
            self._sweep_now.set()
            log.info("VK: вошли как %s", self.vk.me_name)
            if recovered:
                await self._notify(f"✅ ВК подключён: {esc(self.vk.me_name)}")

    async def _wait_new_session(self) -> None:
        """Ждать, пока login перезапишет файл сессии (или истечёт AUTH_RECHECK)."""
        deadline = time.monotonic() + self.AUTH_RECHECK
        while time.monotonic() < deadline and not self.vk.session_changed():
            await asyncio.sleep(self.AUTH_RETRY)

    async def _notify(self, text: str) -> None:
        try:
            await self.sink.notice(text)
        except Exception as e:
            log.warning("Не удалось отправить уведомление в Telegram: %s", e)

    # --- источники сигналов ----------------------------------------------------

    async def _longpoll_loop(self) -> None:
        lp = LongPoll(self.vk)
        backoff = 1.0
        while True:
            await self._authed.wait()
            if not self.state.watches:
                # Ничего не выбрано — не держим соединение с ВК зря
                lp.reset()
                self.lp_mode = None
                await asyncio.sleep(2)
                continue
            try:
                if not lp.connected:
                    await lp.connect()
                    self.lp_mode = lp.mode
                    log.info("VK long poll подключён (режим %s)", lp.mode)
                    self._sweep_now.set()  # догнать то, что пришло, пока не слушали
                updates = await lp.poll()
                backoff = 1.0
                for update in updates:
                    for peer_id in peers_in_update(update, self.state.watches.keys()):
                        self.trigger(peer_id)
                continue
            except VKAuthError as e:
                self._auth_failed(e)
            except LongPollExpired:
                pass
            except LongPollUnavailable as e:
                log.warning("VK long poll недоступен (%s) — работаю опросом", e)
                self.lp_mode = None
                await asyncio.sleep(self.LP_RETRY)
                continue
            except NETWORK_ERRORS as e:
                log.warning("VK long poll: %s", e)
            except Exception:
                log.exception("VK long poll: сбой")
            lp.reset()
            self.lp_mode = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _sweep_loop(self) -> None:
        while True:
            await self._authed.wait()
            self._sweep_now.clear()
            try:
                await self._sweep()
            except VKAuthError as e:
                self._auth_failed(e)
            except NETWORK_ERRORS as e:
                log.warning("VK: сверка чатов не удалась: %s", e)
            except Exception:
                log.exception("VK: сбой при сверке чатов")
            interval = self.cfg.poll_interval if self.lp_mode else self.cfg.fallback_poll_interval
            try:
                await asyncio.wait_for(self._sweep_now.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _sweep(self) -> None:
        """Одним запросом на 100 чатов узнать, где появились новые сообщения."""
        peer_ids = list(self.state.watches)
        for i in range(0, len(peer_ids), 100):
            resp = await self.vk.call("messages.getConversationsById", peer_ids=peer_ids[i : i + 100])
            for conv in (resp or {}).get("items") or []:
                peer_id = (conv.get("peer") or {}).get("id")
                watch = self.state.watches.get(peer_id)
                if watch is None:
                    continue
                last = conv.get("last_conversation_message_id")
                if last is None or watch.last_cmid is None or last > watch.last_cmid:
                    self.trigger(peer_id)

    # --- пересылка -------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            await self._pending_evt.wait()
            await asyncio.sleep(self.DEBOUNCE)
            await self._authed.wait()
            self._pending_evt.clear()
            peers, self._pending = self._pending, set()
            for peer_id in peers:
                try:
                    await self._forward_new(peer_id)
                except VKAuthError as e:
                    self._auth_failed(e)
                    self.trigger(peer_id)
                except TransientSendError as e:
                    # Курсор не сдвинут — сообщение уйдёт при следующей сверке
                    log.warning("Telegram недоступен, повторю позже: %s", e)
                except NETWORK_ERRORS as e:
                    log.warning("VK: не удалось прочитать чат %s: %s", peer_id, e)
                except Exception:
                    log.exception("Сбой при пересылке из чата %s", peer_id)

    async def _forward_new(self, peer_id: int) -> None:
        watch = self.state.watches.get(peer_id)
        if watch is None:
            return
        count = self.cfg.history_count
        resp = await self.vk.call("messages.getHistory", peer_id=peer_id, count=count, extended=1) or {}
        self.names.update(resp)
        for conv in resp.get("conversations") or []:
            info = peer_info(conv, self.names)
            if info.peer_id == peer_id and info.title:
                watch.title = info.title

        items = [m for m in resp.get("items") or [] if isinstance(m.get("conversation_message_id"), int)]
        if watch.last_cmid is None:
            watch.last_cmid = max((m["conversation_message_id"] for m in items), default=0)
            self.state.save()
            return
        new = sorted(
            (m for m in items if m["conversation_message_id"] > watch.last_cmid),
            key=lambda m: m["conversation_message_id"],
        )
        if not new:
            return
        if len(new) == len(items) >= count and new[0]["conversation_message_id"] > watch.last_cmid + 1:
            await self.sink.notice(
                f"… <b>{esc(watch.title)}</b>: сообщений накопилось больше {count}, "
                "более старые пропущены"
            )
        for msg in new:
            if self.state.watches.get(peer_id) is not watch:
                return  # чат убрали из списка, пока шла пересылка
            cmid = msg["conversation_message_id"]
            if not msg.get("out") or self.cfg.forward_outgoing:
                rendered = render_message(msg, title=watch.title, kind=watch.kind, names=self.names)
                await self.sink.send(rendered, url=message_url(self.cfg.vk_origin, peer_id, cmid))
                self.forwarded += 1
                self.last_forward_at = time.time()
            watch.last_cmid = cmid
            self.state.save()

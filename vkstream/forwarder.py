"""Слежение за чатами ВК и пересылка новых сообщений по правилам.

Правила (см. state.DEFAULT_POLICY): личные диалоги пересылаются все, диалоги с
сообществами («бизнес») — нет, про группы и каналы владельца спрашивают при
первом новом сообщении. Решение по конкретному чату перекрывает правило.

Устройство: сверка сравнивает список чатов ВК с тем, что помнит бот, и находит
чаты с новыми сообщениями; сами сообщения читаются из истории по курсору
conversation_message_id. Long poll нужен только как сигнал «пора свериться»,
поэтому формат его событий разбирать не приходится, а пропущенный сигнал
навёрстывается плановой сверкой.
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
from .state import ALLOW, ASK, BLOCK, CHANNEL, PENDING, Chat, State
from .vk.channels import channel_history, list_channels
from .vk.client import VKAuthError, VKClient, VKError
from .vk.longpoll import LongPoll, LongPollExpired, LongPollUnavailable, is_noise, peers_in_update
from .vk.peers import Names, PeerInfo, list_conversations
from .vk.wall import (
    NotACommunity,
    community_name,
    parse_link,
    post_url,
    resolve_community_id,
    wall_key,
    wall_owner,
    wall_posts,
)

log = logging.getLogger(__name__)

NETWORK_ERRORS = (VKError, aiohttp.ClientError, asyncio.TimeoutError, OSError)

LOGIN_HINT = "Выполните на компьютере, где запущен бот:\n<code>python -m vkstream login</code>"

# Ошибки VK API, которые проходят сами: сбой сервера, лимиты запросов, капча
TRANSIENT_VK_CODES = {-1, 1, 6, 9, 10, 14, 29}

# Источники списка чатов; у каждого своя отметка «прочитан целиком» в State.baselined
CONVERSATIONS, REQUESTS, CHANNELS = "conversations", "requests", "channels"
# Стена сообщества, добавленная по ссылке: в списках ВК её нет, Chat.api == WALL
WALL = "wall"


class Sink(Protocol):
    async def send(self, rendered: Rendered, url: str | None = None) -> None: ...
    async def notice(self, text: str) -> None: ...
    async def ask(self, chat: Chat) -> None: ...


class Forwarder:
    DEBOUNCE = 0.3  # сек: даём ВК записать сообщение и склеиваем пачку событий
    MIN_SWEEP_GAP = 1.0  # не чаще одной сверки в секунду, как бы ни сыпались события
    AUTH_RETRY = 3.0  # как часто проверять, не появилась ли новая сессия
    AUTH_RECHECK = 900.0  # как часто перепроверять старую сессию без изменений на диске
    LP_RETRY = 300.0  # когда снова пробовать long poll, если он недоступен
    FEATURE_RETRY = 1800.0  # когда снова пробовать каналы/запросы, если ВК их не отдал
    SWEEP_PAGE = 50
    MAX_CONVERSATIONS = 20_000
    NEW_CHAT_BACKLOG = 10  # сколько непрочитанных пересылать из чата, которого раньше не было
    WALL_PAGE = 10  # сколько последних записей стены запрашивать за раз

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
        # Почему недоступны дополнительные списки (None — работают)
        self.feature_errors: dict[str, str | None] = {REQUESTS: None, CHANNELS: None}
        # Последняя ошибка чтения истории по каждому API (None — читается)
        self.read_errors: dict[str, str | None] = {}
        self._feature_retry_at: dict[str, float] = {}
        self._last_full_sweep: float | None = None
        self._wall_polled_at: dict[int, float] = {}
        self._dirty = False
        self._authed = asyncio.Event()
        self._need_auth = asyncio.Event()
        self._need_auth.set()
        self._pending: dict[int, bool] = {}  # peer_id -> читать ли без оглядки на список
        self._pending_evt = asyncio.Event()
        self._sweep_now = asyncio.Event()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._auth_loop())
            tg.create_task(self._longpoll_loop())
            tg.create_task(self._sweep_loop())
            tg.create_task(self._worker())

    # --- решения владельца (вызывается из бота) --------------------------------

    def decide(self, peer_id: int, decision: str) -> Chat | None:
        """Запомнить решение по чату: ALLOW или BLOCK."""
        chat = self.state.chats.get(peer_id)
        if chat is None:
            return None
        chat.decision, chat.asked = decision, False
        if decision == BLOCK:
            chat.last_cmid = None  # придержанное больше не нужно; см. _note
        self.state.save()
        if decision == ALLOW:
            # Догнать придержанное до ответа либо (после блокировки) встать на
            # текущее последнее сообщение. Список чатов мог устареть: события по
            # неинтересным чатам сверку не будят, — поэтому читаем историю сразу.
            self.trigger(peer_id, force=True)
        return chat

    async def add_wall(self, link: str) -> Chat:
        """Подписаться на стену сообщества по ссылке; пересылаться будут новые записи."""
        screen_name = parse_link(link, bare_names=True)
        if screen_name is None:
            raise NotACommunity(link)
        group_id = await resolve_community_id(self.vk, screen_name)
        owner_id = -group_id
        # Читаем стену сразу: так и проверяем, что она нам открыта, и узнаём,
        # с какой записи начинать
        posts = await wall_posts(self.vk, self.names, owner_id, self.WALL_PAGE)
        newest = max((post["conversation_message_id"] for post in posts), default=0)
        title = await community_name(self.vk, self.names, group_id)
        key = wall_key(owner_id)
        chat = self.state.chats.setdefault(key, Chat(key, title, CHANNEL, api=WALL))
        chat.title, chat.decision, chat.asked = title, ALLOW, False
        chat.last_cmid = chat.head_cmid = newest
        chat.last_date = int(time.time())  # только что добавленное — наверху списка
        self._wall_polled_at[key] = time.monotonic()
        self.state.save()
        return chat

    def toggle(self, peer_id: int) -> Chat | None:
        chat = self.state.chats.get(peer_id)
        if chat is None:
            return None
        return self.decide(peer_id, BLOCK if chat.policy == ALLOW else ALLOW)

    def trigger(self, peer_id: int, force: bool = False) -> None:
        """Поставить чат в очередь на чтение. force — читать, даже если новостей не видно."""
        self._pending[peer_id] = force or self._pending.get(peer_id, False)
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

    # --- long poll: сигнал «пора свериться» ------------------------------------

    def _wakes(self, update: object) -> bool:
        """Стоит ли из-за этого события идти в ВК за списком чатов."""
        if is_noise(update):
            return False
        mentioned = peers_in_update(update, self.state.chats.keys())
        # Незнакомый чат или непонятное событие — сверяемся. Знакомые — только если
        # хоть один из них нам интересен: иначе шумная заблокированная группа
        # заставляла бы ходить в ВК на каждое своё сообщение.
        return not mentioned or any(self.state.chats[p].policy in (ALLOW, ASK) for p in mentioned)

    async def _longpoll_loop(self) -> None:
        lp = LongPoll(self.vk)
        backoff = 1.0
        while True:
            await self._authed.wait()
            try:
                if not lp.connected:
                    await lp.connect()
                    self.lp_mode = lp.mode
                    log.info("VK long poll подключён (режим %s)", lp.mode)
                    self._sweep_now.set()  # догнать то, что пришло, пока не слушали
                updates = await lp.poll()
                backoff = 1.0
                if any(self._wakes(update) for update in updates):
                    self._sweep_now.set()
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

    # --- сверка списка чатов ---------------------------------------------------

    async def _sweep_loop(self) -> None:
        while True:
            await self._authed.wait()
            self._sweep_now.clear()
            started = time.monotonic()
            try:
                await self._sweep()
            except VKAuthError as e:
                self._auth_failed(e)
            except NETWORK_ERRORS as e:
                log.warning("VK: сверка чатов не удалась: %s", e)
            except Exception:
                log.exception("VK: сбой при сверке чатов")
            self._flush()
            interval = self.cfg.poll_interval if self.lp_mode else self.cfg.fallback_poll_interval
            try:
                await asyncio.wait_for(self._sweep_now.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            pause = max(self.DEBOUNCE, self.MIN_SWEEP_GAP - (time.monotonic() - started))
            await asyncio.sleep(pause)

    async def _sweep(self) -> None:
        now = time.monotonic()
        full = self._last_full_sweep is None or now - self._last_full_sweep >= self.cfg.poll_interval
        await self._sweep_conversations()
        if full:
            # Запросы на переписку и каналы в основной список не входят и своих
            # сигналов не шлют — их сверяем только по расписанию.
            await self._sweep_feature(REQUESTS, self._list_requests)
            await self._sweep_feature(CHANNELS, self._list_channels)
            self._last_full_sweep = now
        # В очередь идут все чаты с непрочитанным ботом хвостом, а не только
        # замеченные сейчас: так повторяется и то, что не удалось переслать раньше.
        for chat in self.state.chats.values():
            if chat.api == WALL:
                # У стен списка нет: читаем саму стену, когда подошёл срок
                polled_at = self._wall_polled_at.get(chat.peer_id)
                due = polled_at is None or now - polled_at >= self.cfg.wall_poll_interval
                if chat.policy == ALLOW and due:
                    self._wall_polled_at[chat.peer_id] = now
                    self.trigger(chat.peer_id, force=True)
            elif chat.has_news and chat.policy in (ALLOW, ASK):
                self.trigger(chat.peer_id)

    async def _sweep_conversations(self) -> None:
        baseline = CONVERSATIONS not in self.state.baselined
        page = 200 if baseline else self.SWEEP_PAGE
        offset = 0
        while offset < self.MAX_CONVERSATIONS:
            items, total = await list_conversations(self.vk, self.names, offset, page)
            changed = False
            for info in items:
                known = self.state.chats.get(info.peer_id)
                if known is not None and known.api != info.api:
                    continue  # канал, попавший в общий список: его ведёт сверка каналов
                changed = self._note(info, CONVERSATIONS)
            offset += len(items)
            # Список отсортирован по свежести: листаем дальше, только пока есть изменения
            if not items or offset >= total or not (baseline or changed):
                break
        if baseline:
            self.state.baselined.add(CONVERSATIONS)
            self._dirty = True
            log.info("VK: прочитан список чатов (%d)", len(self.state.chats))

    async def _list_requests(self) -> list[PeerInfo]:
        """Папка «Запросы»: сообщения от тех, кого нет в друзьях."""
        items, _ = await list_conversations(self.vk, self.names, 0, self.SWEEP_PAGE, filter="message_request")
        return items

    async def _list_channels(self) -> list[PeerInfo]:
        return await list_channels(self.vk, self.names)

    async def _sweep_feature(self, source: str, fetch) -> None:
        """Сверить дополнительный список. Если ВК его не отдаёт — работаем без него."""
        if time.monotonic() < self._feature_retry_at.get(source, 0.0):
            return
        try:
            items = await fetch()
        except VKAuthError:
            raise
        except VKError as e:
            if self.feature_errors[source] is None:
                log.warning("VK: список «%s» недоступен: %s", source, e)
            self.feature_errors[source] = str(e)
            self._feature_retry_at[source] = time.monotonic() + self.FEATURE_RETRY
            return
        self.feature_errors[source] = None
        for info in items:
            self._note(info, source)
        if source not in self.state.baselined:
            self.state.baselined.add(source)
            self._dirty = True

    def _note(self, info: PeerInfo, source: str) -> bool:
        """Сверить запись о чате со строкой списка ВК. True — в чате что-то изменилось."""
        chat = self.state.chats.get(info.peer_id)
        if chat is None:
            cursor = info.last_cmid
            if source in self.state.baselined and cursor is not None:
                # Чата не было при первом знакомстве со списком, значит он новый:
                # пересылаем то, что в нём ещё не прочитано.
                cursor = max(0, cursor - min(max(info.unread, 1), self.NEW_CHAT_BACKLOG))
            chat = Chat(info.peer_id, info.title, info.kind, info.api, cursor, info.last_cmid)
            chat.last_date = info.last_date
            self.state.chats[info.peer_id] = chat
            self._dirty = True
            changed = True
        else:
            if chat.api != info.api:
                # Запись оказалась каналом: у него своя нумерация постов, начинаем с текущего
                chat.api, chat.last_cmid, chat.head_cmid = info.api, info.last_cmid, None
            changed = info.last_cmid is not None and info.last_cmid != chat.head_cmid
            fresh = (info.title or chat.title, info.kind, info.last_date or chat.last_date)
            if changed or fresh != (chat.title, chat.kind, chat.last_date):
                chat.title, chat.kind, chat.last_date = fresh
                if info.last_cmid is not None:
                    chat.head_cmid = info.last_cmid
                self._dirty = True
        if chat.policy == BLOCK and chat.last_cmid is not None:
            # За заблокированным чатом не следим вовсе: курсор сброшен, и после
            # разблокировки пересылка начнётся с сообщений, пришедших позже.
            chat.last_cmid = None
            self._dirty = True
        return changed

    @staticmethod
    def _skip_to_head(chat: Chat) -> None:
        if chat.head_cmid is not None:
            chat.last_cmid = max(chat.last_cmid or 0, chat.head_cmid)

    def _flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.state.save()

    # --- пересылка -------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            await self._pending_evt.wait()
            await self._authed.wait()
            self._pending_evt.clear()
            peers, self._pending = self._pending, {}
            for peer_id, force in peers.items():
                try:
                    await self._process(peer_id, force)
                except VKAuthError as e:
                    self._auth_failed(e)
                except TransientSendError as e:
                    # Курсор не сдвинут — сообщение уйдёт при следующей сверке
                    log.warning("Telegram недоступен, повторю позже: %s", e)
                except VKError as e:
                    log.warning("VK: не удалось прочитать чат %s: %s", peer_id, e)
                    self._read_failed(peer_id, e)
                except NETWORK_ERRORS as e:
                    log.warning("VK: не удалось прочитать чат %s: %s", peer_id, e)
                except Exception:
                    log.exception("Сбой при пересылке из чата %s", peer_id)

    def _read_failed(self, peer_id: int, error: VKError) -> None:
        self.read_errors[getattr(self.state.chats.get(peer_id), "api", "messages")] = str(error)
        if error.code in TRANSIENT_VK_CODES:
            return  # временное: чат останется в очереди до следующей сверки
        # ВК отказал по существу (например, нас исключили из беседы) — повтор не
        # поможет, поэтому считаем эти сообщения обработанными.
        chat = self.state.chats.get(peer_id)
        if chat is not None:
            self._skip_to_head(chat)
            self.state.save()

    async def _history(self, chat: Chat, count: int, fresh: bool) -> list[dict]:
        if chat.api == WALL:
            items = await wall_posts(self.vk, self.names, wall_owner(chat.peer_id), count)
        elif chat.api == CHANNELS:
            # fresh: номер последнего поста из списка мог устареть — пусть узнают заново
            head = None if fresh else chat.head_cmid
            items = await channel_history(self.vk, self.names, chat.peer_id, head, count)
        else:
            resp = await self.vk.call("messages.getHistory", peer_id=chat.peer_id, count=count, extended=1) or {}
            self.names.update(resp)
            items = resp.get("items") or []
        self.read_errors[chat.api] = None
        return items

    async def _process(self, peer_id: int, force: bool = False) -> None:
        chat = self.state.chats.get(peer_id)
        if chat is None or chat.policy not in (ALLOW, ASK) or not (chat.has_news or force):
            return
        count = self.WALL_PAGE if chat.api == WALL else self.cfg.history_count
        history = await self._history(chat, count, fresh=force)
        items = [m for m in history if isinstance(m.get("conversation_message_id"), int)]
        newest = max((m["conversation_message_id"] for m in items), default=0)
        if chat.head_cmid is None or newest > chat.head_cmid:
            chat.head_cmid = newest
        if chat.last_cmid is None:
            chat.last_cmid = newest
            self.state.save()
            return
        new = sorted(
            (m for m in items if m["conversation_message_id"] > chat.last_cmid),
            key=lambda m: m["conversation_message_id"],
        )
        if not new:
            # Список обещал новое, а в истории его нет (удалили): не ходим по кругу
            if chat.has_news:
                self._skip_to_head(chat)
                self.state.save()
            return

        if chat.policy == ASK:
            first = next((m for m in new if not m.get("out")), None)
            if first is None:  # в чате писали только вы сами — спрашивать пока не о чем
                chat.last_cmid = new[-1]["conversation_message_id"]
            else:
                await self._forward(chat, first)
                await self.sink.ask(chat)
                # Остальное придержим до ответа: курсор остаётся на первом сообщении
                chat.last_cmid, chat.asked = first["conversation_message_id"], True
            self.state.save()
            return

        if len(new) == len(items) >= count and new[0]["conversation_message_id"] > chat.last_cmid + 1:
            await self.sink.notice(
                f"… <b>{esc(chat.title)}</b>: сообщений накопилось больше {count}, "
                "более старые пропущены"
            )
        for msg in new:
            if self.state.chats.get(peer_id) is not chat or chat.policy != ALLOW:
                return  # решение по чату изменили, пока шла пересылка
            if not msg.get("out") or self.cfg.forward_outgoing:
                await self._forward(chat, msg)
            chat.last_cmid = msg["conversation_message_id"]
            self.state.save()

    async def _forward(self, chat: Chat, msg: dict) -> None:
        rendered = render_message(msg, title=chat.title, kind=chat.kind, names=self.names)
        cmid = msg["conversation_message_id"]
        if chat.api == WALL:
            url = post_url(self.cfg.vk_origin, wall_owner(chat.peer_id), cmid)
        elif chat.api == CHANNELS:
            url = None  # адрес поста канала в веб-версии нам неизвестен
        else:
            url = message_url(self.cfg.vk_origin, chat.peer_id, cmid)
        await self.sink.send(rendered, url=url)
        chat.last_date = max(chat.last_date, int(msg.get("date") or 0))
        self.forwarded += 1
        self.last_forward_at = time.time()

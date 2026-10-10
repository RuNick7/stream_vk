"""Точка входа: `python -m vkstream [run|login|check]`."""

from __future__ import annotations

import argparse
import asyncio
import logging

from .config import Config, load_config

log = logging.getLogger("vkstream")


def _make_bot(cfg: Config):
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.enums import ParseMode

    if not cfg.tg_token:
        raise SystemExit("Не задан TG_BOT_TOKEN — скопируйте .env.example в .env и впишите токен бота.")
    session = AiohttpSession(proxy=cfg.tg_proxy) if cfg.tg_proxy else None
    return Bot(cfg.tg_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def run(cfg: Config, handle_signals: bool = True) -> None:
    from aiogram import Dispatcher

    from .bot import COMMANDS, App, build_router
    from .forwarder import Forwarder
    from .sender import TelegramSink
    from .state import State
    from .vk.client import VKClient

    bot = _make_bot(cfg)
    state = State.load(cfg.state_path)
    if cfg.tg_owner_id is not None:
        state.owner_id = cfg.tg_owner_id
    vk = VKClient(cfg)
    forwarder = Forwarder(cfg, state, vk, TelegramSink(bot, state, vk))
    app = App(cfg, state, vk, forwarder)

    dp = Dispatcher()
    dp.include_router(build_router())
    try:
        me = await bot.get_me()
        await bot.set_my_commands(COMMANDS)
        log.info("Бот запущен: @%s", me.username)
        if state.owner_id is None:
            log.warning(
                "Бот ещё не привязан к владельцу. Откройте ссылку и нажмите «Запустить»:\n"
                "    https://t.me/%s?start=%s",
                me.username,
                app.pair_code,
            )
        task = asyncio.create_task(forwarder.run())
        try:
            await dp.start_polling(bot, app=app, handle_signals=handle_signals)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await vk.close()
        await bot.session.close()


async def check(cfg: Config) -> None:
    """Проверить по шагам, что всё настроено: удобно при первом запуске."""
    from .vk.client import VKAuthError, VKClient, VKError
    from .vk.longpoll import LongPoll, LongPollUnavailable
    from .vk.peers import Names, list_conversations

    vk = VKClient(cfg)
    try:
        try:
            await vk.authorize()
            print(f"[ok] ВК: вошли как {vk.me_name} (id{vk.user_id})")
            _, total = await list_conversations(vk, Names(), 0, 1)
            print(f"[ok] ВК: список чатов читается, диалогов: {total}")
            lp = LongPoll(vk)
            try:
                await lp.connect()
                print(f"[ok] ВК: long poll подключается (режим {lp.mode})")
            except LongPollUnavailable as e:
                print(f"[!!] ВК: long poll недоступен, бот будет работать опросом. Причина: {e}")
        except VKAuthError as e:
            print(f"[!!] ВК не подключён: {e.message}. Выполните: python -m vkstream login")
        except VKError as e:
            print(f"[!!] ВК: {e}")
    finally:
        await vk.close()

    if not cfg.tg_token:
        print("[!!] Telegram: не задан TG_BOT_TOKEN")
        return
    bot = _make_bot(cfg)
    try:
        me = await bot.get_me()
        print(f"[ok] Telegram: бот @{me.username}")
    except Exception as e:
        print(f"[!!] Telegram: {e}")
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m vkstream", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="запустить бота (по умолчанию)")
    login_parser = sub.add_parser("login", help="подключить аккаунт ВК")
    login_parser.add_argument(
        "--cookie", action="store_true", help="вставить cookie вручную вместо входа в окне браузера"
    )
    sub.add_parser("check", help="проверить подключение к ВК и Telegram")
    args = parser.parse_args()

    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    if args.command == "login":
        from .login import login

        asyncio.run(login(cfg, from_cookie=args.cookie))
    elif args.command == "check":
        asyncio.run(check(cfg))
    else:
        try:
            asyncio.run(run(cfg))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

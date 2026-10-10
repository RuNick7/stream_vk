"""Настройки из переменных окружения / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    tg_token: str = ""
    tg_owner_id: int | None = None
    tg_proxy: str | None = None
    vk_proxy: str | None = None
    data_dir: Path = Path("data")
    forward_outgoing: bool = False
    # Интервал сверки с ВК, пока long poll работает, и пока он недоступен
    poll_interval: float = 60.0
    fallback_poll_interval: float = 8.0
    # Сколько последних сообщений чата запрашивать за раз (максимум VK — 200)
    history_count: int = 50
    log_level: str = "INFO"
    # Параметры веб-клиента vk.ru. Менять, только если VK их переименует.
    vk_origin: str = "https://vk.ru"
    vk_login_base: str = "https://login.vk.ru"
    vk_api_base: str = "https://web.api.vk.ru/method"
    vk_app_id: str = "6287487"
    vk_api_version: str = "5.285"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "vk_session.json"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "да")


def load_config(env_file: str | Path | None = None) -> Config:
    load_dotenv(env_file)
    defaults = Config()
    owner = _str("TG_OWNER_ID")
    return Config(
        tg_token=_str("TG_BOT_TOKEN"),
        tg_owner_id=int(owner) if owner else None,
        tg_proxy=_str("TG_PROXY") or None,
        vk_proxy=_str("VK_PROXY") or None,
        data_dir=Path(_str("DATA_DIR", "data")),
        forward_outgoing=_bool("FORWARD_OUTGOING", defaults.forward_outgoing),
        poll_interval=float(_str("POLL_INTERVAL") or defaults.poll_interval),
        fallback_poll_interval=float(
            _str("FALLBACK_POLL_INTERVAL") or defaults.fallback_poll_interval
        ),
        history_count=min(200, int(_str("HISTORY_COUNT") or defaults.history_count)),
        log_level=_str("LOG_LEVEL", defaults.log_level).upper(),
        vk_origin=_str("VK_ORIGIN", defaults.vk_origin).rstrip("/"),
        vk_login_base=_str("VK_LOGIN_BASE", defaults.vk_login_base).rstrip("/"),
        vk_api_base=_str("VK_API_BASE", defaults.vk_api_base).rstrip("/"),
        vk_app_id=_str("VK_APP_ID", defaults.vk_app_id),
        vk_api_version=_str("VK_API_VERSION", defaults.vk_api_version),
    )

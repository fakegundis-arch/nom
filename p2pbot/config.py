"""Settings loaded from environment variables (or a .env file on the VPS)."""

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required setting: {name}")
    return value or ""


def _bool(name: str, default: bool) -> bool:
    return _get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Bybit
    bybit_api_key: str
    bybit_api_secret: str
    bybit_base_url: str

    # Nomba
    nomba_client_id: str
    nomba_client_secret: str
    nomba_account_id: str
    nomba_base_url: str
    nomba_webhook_secret: str

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # Bot behaviour
    db_path: str
    poll_seconds: int
    webhook_host: str
    webhook_port: int
    payment_window_minutes: int
    naira_low_threshold: Decimal
    usdt_low_threshold: Decimal

    # Safety switch. Phase 1 never moves money: the bot only watches,
    # matches and alerts. Release/pay/transfer code refuses to run while
    # this is True.
    monitor_only: bool


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        bybit_api_key=_get("BYBIT_API_KEY", required=True),
        bybit_api_secret=_get("BYBIT_API_SECRET", required=True),
        bybit_base_url=_get("BYBIT_BASE_URL", "https://api.bybit.com"),
        nomba_client_id=_get("NOMBA_CLIENT_ID", required=True),
        nomba_client_secret=_get("NOMBA_CLIENT_SECRET", required=True),
        nomba_account_id=_get("NOMBA_ACCOUNT_ID", required=True),
        nomba_base_url=_get("NOMBA_BASE_URL", "https://sandbox.nomba.com"),
        nomba_webhook_secret=_get("NOMBA_WEBHOOK_SECRET", required=True),
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN", required=True),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID", required=True),
        db_path=_get("DB_PATH", "data/p2pbot.sqlite3"),
        poll_seconds=int(_get("POLL_SECONDS", "5")),
        # Listen on localhost only; Caddy in front handles HTTPS (see docs/DEPLOY.md).
        webhook_host=_get("WEBHOOK_HOST", "127.0.0.1"),
        webhook_port=int(_get("WEBHOOK_PORT", "8080")),
        payment_window_minutes=int(_get("PAYMENT_WINDOW_MINUTES", "30")),
        naira_low_threshold=Decimal(_get("NAIRA_LOW_THRESHOLD", "500000")),
        usdt_low_threshold=Decimal(_get("USDT_LOW_THRESHOLD", "300")),
        monitor_only=_bool("MONITOR_ONLY", True),
    )

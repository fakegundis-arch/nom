"""Settings loaded from environment variables (or a .env file on the VPS)."""

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    # A blank line in .env (e.g. "SELL_MIN_PRICE=") means "use the default".
    value = os.environ.get(name) or default
    if required and not value:
        raise RuntimeError(f"Missing required setting: {name}")
    return value or ""


def _bool(name: str, default: bool) -> bool:
    return _get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AdSettings:
    """Ad repricing and inventory. Prices are naira per USDT."""

    # Off: the bot only suggests ad changes on Telegram. On: it edits your ads.
    # Editing ads never moves money, so this is separate from MONITOR_ONLY.
    auto_ads: bool = False
    # Hard limits. Repricing stays off until both are set.
    sell_min_price: Decimal = Decimal("0")   # never sell USDT cheaper than this
    buy_max_price: Decimal = Decimal("0")    # never buy USDT dearer than this
    min_spread: Decimal = Decimal("15")      # sell price stays at least this far above buy price
    price_step: Decimal = Decimal("1")       # how much to beat a competitor by
    target_rank: int = 2                     # 1 = best price on the page, 2 = second best...
    reprice_seconds: int = 60
    min_competitor_usdt: Decimal = Decimal("50")  # ignore tiny "bait" ads
    naira_buffer: Decimal = Decimal("100000")     # naira the BUY ad never offers
    buy_max_order: Decimal = Decimal("0")         # cap on one BUY order in naira (0 = no cap)
    token: str = "USDT"
    currency: str = "NGN"


@dataclass(frozen=True)
class AutoSettings:
    """Automatic release and payment. Both need MONITOR_ONLY=false as well."""

    auto_release: bool = False              # release USDT when the buyer's payment is confirmed
    auto_pay: bool = False                  # pay sellers from Nomba and mark the order paid
    max_order: Decimal = Decimal("500000")  # bigger orders wait for /release or /pay from you
    new_counterparty_max: Decimal = Decimal("100000")  # cap for people you've never traded with
    daily_limit: Decimal = Decimal("5000000")          # total naira the bot may handle alone per day


@dataclass(frozen=True)
class RefillSettings:
    """Turn USDT into naira through Nomba when naira runs low."""

    auto_refill: bool = False
    target_naira: Decimal = Decimal("2000000")   # refill up to this much free naira
    min_rate: Decimal = Decimal("0")             # never convert below this many naira per USDT
    min_usdt: Decimal = Decimal("20")            # don't bother converting less than this
    max_usdt_per_day: Decimal = Decimal("1000")
    check_seconds: int = 300
    # Nomba Global Payout fields for the USDT -> NGN exchange.
    business_name: str = ""                      # your business name, sent as sender/receiver
    source_country: str = "NG"
    # Optional: let the bot move USDT from Bybit to your Nomba USDT address.
    # Uses a SEPARATE Bybit key that can only withdraw to whitelisted addresses.
    bybit_withdraw_key: str = ""
    bybit_withdraw_secret: str = ""
    nomba_usdt_address: str = ""
    nomba_usdt_chain: str = "TRX"


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

    ads: AdSettings = AdSettings()
    auto: AutoSettings = AutoSettings()
    refill: RefillSettings = RefillSettings()


def load_ad_settings() -> AdSettings:
    return AdSettings(
        auto_ads=_bool("AUTO_ADS", False),
        sell_min_price=Decimal(_get("SELL_MIN_PRICE", "0")),
        buy_max_price=Decimal(_get("BUY_MAX_PRICE", "0")),
        min_spread=Decimal(_get("MIN_SPREAD", "15")),
        price_step=Decimal(_get("PRICE_STEP", "1")),
        target_rank=int(_get("TARGET_RANK", "2")),
        reprice_seconds=int(_get("REPRICE_SECONDS", "60")),
        min_competitor_usdt=Decimal(_get("MIN_COMPETITOR_USDT", "50")),
        naira_buffer=Decimal(_get("NAIRA_BUFFER", "100000")),
        buy_max_order=Decimal(_get("BUY_MAX_ORDER", "0")),
    )


def load_auto_settings() -> AutoSettings:
    return AutoSettings(
        auto_release=_bool("AUTO_RELEASE", False),
        auto_pay=_bool("AUTO_PAY", False),
        max_order=Decimal(_get("AUTO_MAX_ORDER", "500000")),
        new_counterparty_max=Decimal(_get("AUTO_NEW_COUNTERPARTY_MAX", "100000")),
        daily_limit=Decimal(_get("AUTO_DAILY_LIMIT", "5000000")),
    )


def load_refill_settings() -> RefillSettings:
    return RefillSettings(
        auto_refill=_bool("AUTO_REFILL", False),
        target_naira=Decimal(_get("REFILL_TARGET_NAIRA", "2000000")),
        min_rate=Decimal(_get("REFILL_MIN_RATE", "0")),
        min_usdt=Decimal(_get("REFILL_MIN_USDT", "20")),
        max_usdt_per_day=Decimal(_get("REFILL_MAX_USDT_PER_DAY", "1000")),
        check_seconds=int(_get("REFILL_CHECK_SECONDS", "300")),
        business_name=_get("BUSINESS_NAME", ""),
        source_country=_get("REFILL_SOURCE_COUNTRY", "NG"),
        bybit_withdraw_key=_get("BYBIT_WITHDRAW_API_KEY", ""),
        bybit_withdraw_secret=_get("BYBIT_WITHDRAW_API_SECRET", ""),
        nomba_usdt_address=_get("NOMBA_USDT_ADDRESS", ""),
        nomba_usdt_chain=_get("NOMBA_USDT_CHAIN", "TRX"),
    )


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
        ads=load_ad_settings(),
        auto=load_auto_settings(),
        refill=load_refill_settings(),
    )

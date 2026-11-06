from decimal import Decimal
from pathlib import Path

from p2pbot.config import load_settings

REQUIRED = ["BYBIT_API_KEY", "BYBIT_API_SECRET", "NOMBA_CLIENT_ID", "NOMBA_CLIENT_SECRET",
            "NOMBA_ACCOUNT_ID", "NOMBA_WEBHOOK_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def test_env_example_loads_with_safe_defaults(monkeypatch, tmp_path):
    # Load .env.example exactly as a new user would copy it, with blanks left blank.
    for line in Path(".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
    for name in REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.chdir(tmp_path)  # no real .env here
    s = load_settings()
    assert s.monitor_only is True
    assert not (s.auto.auto_release or s.auto.auto_pay or s.refill.auto_refill or s.ads.auto_ads)
    assert s.ads.sell_min_price == Decimal("0")
    assert s.refill.min_rate == Decimal("0")
    assert s.nomba_base_url == "https://sandbox.nomba.com"

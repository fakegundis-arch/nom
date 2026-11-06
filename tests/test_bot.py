import json
from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot.bot import Bot, find_bank_code
from p2pbot.bybit_client import (SIDE_BUY, SIDE_SELL, STATUS_WAITING_BUYER_PAY, STATUS_WAITING_SELLER_RELEASE,
                                 BybitError, BybitP2PClient, P2POrder, PaymentTerm, sign)
from p2pbot.config import Settings
from p2pbot.db import Database
from p2pbot.nomba_client import NombaClient, NombaError
from p2pbot.webhook import compute_signature

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "nomba-webhook.json").read_text())
KEY = "test-signing-key"
ORDER_MS = 1_759_142_000_000  # shortly before the fixture's 2025-09-29T10:51:44Z credit


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)


class FakeNomba:
    def __init__(self, txn_amount="10", status="SUCCESS"):
        self.txn = {"amount": txn_amount, "status": status}

    def get_transaction(self, ref):
        return self.txn

    def banks(self):
        return [{"code": "058", "name": "Guaranty Trust Bank"}, {"code": "044", "name": "Access Bank"}]

    def lookup_account_name(self, account_no, code):
        return {"0123456789": "ADEBAYO TUNDE"}.get(account_no, "SOMEONE ELSE")


def settings(**overrides):
    base = dict(bybit_api_key="k", bybit_api_secret="s", bybit_base_url="https://x", nomba_client_id="c",
                nomba_client_secret="s", nomba_account_id="a", nomba_base_url="https://x",
                nomba_webhook_secret=KEY, telegram_bot_token="t", telegram_chat_id="1", db_path=":memory:",
                poll_seconds=1, webhook_host="127.0.0.1", webhook_port=0, payment_window_minutes=30,
                naira_low_threshold=Decimal("500000"), usdt_low_threshold=Decimal("300"), monitor_only=True)
    base.update(overrides)
    return Settings(**base)


def make_bot(nomba=None):
    bot = Bot(settings(), bybit=None, nomba=nomba or FakeNomba(), db=Database(":memory:"), telegram=FakeTelegram())
    bot.webhook_tolerance = None  # the fixture's timestamp is in the past
    return bot


def deliver(bot, event):
    ts = "2025-09-29T10:51:44Z"
    hdrs = {"nomba-signature": compute_signature(event, ts, KEY), "nomba-timestamp": ts}
    return bot.on_webhook(json.dumps(event).encode(), hdrs)


def sell_order(status, amount="10", buyer="John Doe"):
    return P2POrder("o1", SIDE_SELL, status, "USDT", "NGN", Decimal("1600"), Decimal("0.00625"),
                    Decimal(amount), buyer, "Me", "buyer_nick", ORDER_MS, [])


def test_confirmed_credit_is_stored_and_announced():
    bot = make_bot()
    assert deliver(bot, FIXTURE) == 200
    assert any("₦10 received from John Doe" in m for m in bot.tg.sent)
    # The same webhook again is not stored or announced twice.
    deliver(bot, FIXTURE)
    assert sum("received from" in m for m in bot.tg.sent) == 1


def test_amount_comes_from_nomba_not_the_webhook():
    bot = make_bot(FakeNomba(txn_amount="999999"))
    deliver(bot, FIXTURE)
    assert any("₦999999 received" in m for m in bot.tg.sent)


def test_unsuccessful_transaction_is_not_stored():
    bot = make_bot(FakeNomba(status="PAYMENT_FAILED"))
    deliver(bot, FIXTURE)
    assert bot.db.credits_since(0) == []


def test_bad_signature_returns_401():
    bot = make_bot()
    assert bot.on_webhook(json.dumps(FIXTURE).encode(), {"nomba-signature": "x", "nomba-timestamp": "y"}) == 401


def test_sell_order_paid_by_buyer_is_confirmed_once():
    bot = make_bot()
    deliver(bot, FIXTURE)
    bot.process_order(sell_order(STATUS_WAITING_SELLER_RELEASE))
    bot.process_order(sell_order(STATUS_WAITING_SELLER_RELEASE))
    confirmations = [m for m in bot.tg.sent if "payment confirmed" in m]
    assert len(confirmations) == 1
    assert "Release in Bybit or send /release o1" in confirmations[0]


def test_sell_order_paid_by_stranger_needs_review():
    bot = make_bot()
    deliver(bot, FIXTURE)
    bot.process_order(sell_order(STATUS_WAITING_SELLER_RELEASE, buyer="Mary Jane"))
    assert any("third-party" in m for m in bot.tg.sent)
    assert not any("payment confirmed" in m for m in bot.tg.sent)


def test_sell_order_marked_paid_without_money_warns_not_to_release():
    bot = make_bot()
    bot.process_order(sell_order(STATUS_WAITING_SELLER_RELEASE))
    assert any("Do NOT release" in m for m in bot.tg.sent)


def test_one_credit_cannot_pay_two_orders():
    bot = make_bot()
    deliver(bot, FIXTURE)
    bot.process_order(sell_order(STATUS_WAITING_SELLER_RELEASE))
    second = sell_order(STATUS_WAITING_SELLER_RELEASE)
    second.order_id = "o2"
    bot.process_order(second)
    assert sum("payment confirmed" in m for m in bot.tg.sent) == 1
    assert any("SELL o2" in m and "Do NOT release" in m for m in bot.tg.sent)


def test_buy_order_checks_seller_account_name():
    bot = make_bot()
    order = P2POrder("b1", SIDE_BUY, STATUS_WAITING_BUYER_PAY, "USDT", "NGN", Decimal("1580"), Decimal("400"),
                     Decimal("632000"), "Me", "Tunde Adebayo", "seller_nick", ORDER_MS,
                     [PaymentTerm("Tunde Adebayo", "0123456789", "GTBank Guaranty Trust")])
    bot.process_order(order)
    assert any("ADEBAYO TUNDE" in m and "→ OK" in m for m in bot.tg.sent)

    order.order_id = "b2"
    order.payment_terms = [PaymentTerm("Tunde Adebayo", "9999999999", "Access Bank")]
    bot.process_order(order)
    assert any("SOMEONE ELSE" in m and "MISMATCH" in m for m in bot.tg.sent)


def test_find_bank_code():
    banks = FakeNomba().banks()
    assert find_bank_code("GTBank Guaranty Trust", banks) == "058"
    assert find_bank_code("access", banks) == "044"
    assert find_bank_code("Unknown Microfinance", banks) is None


def test_low_balance_alerts_once_and_on_recovery():
    bot = make_bot()
    bot._low_alert("naira", Decimal("100"), Decimal("500000"), "₦")
    bot._low_alert("naira", Decimal("90"), Decimal("500000"), "₦")
    bot._low_alert("naira", Decimal("900000"), Decimal("500000"), "₦")
    assert sum("low" in m for m in bot.tg.sent) == 1
    assert sum("back above" in m for m in bot.tg.sent) == 1


def test_money_moving_calls_refuse_in_monitor_only_mode():
    with pytest.raises(BybitError):
        BybitP2PClient("k", "s", "https://x").release_assets("o1")
    with pytest.raises(BybitError):
        BybitP2PClient("k", "s", "https://x").mark_paid("o1", "14", "p1")
    with pytest.raises(NombaError):
        NombaClient("c", "s", "a", "https://x").transfer_to_bank(Decimal("1"), "0", "n", "058", "r", "x")


def test_bybit_signature():
    import hashlib, hmac
    expected = hmac.new(b"secret", b"1700000000000key5000{}", hashlib.sha256).hexdigest()
    assert sign("secret", "1700000000000", "key", "5000", "{}") == expected


class FakeBybit:
    def __init__(self, pending, details):
        self.pending, self.details = pending, details

    def pending_order_ids(self):
        return self.pending

    def order_detail(self, order_id):
        return self.details[order_id]


def buy_order(order_id, status, amount):
    return P2POrder(order_id, SIDE_BUY, status, "USDT", "NGN", Decimal("1580"), Decimal("1"),
                    Decimal(amount), "Me", "Seller", "nick", ORDER_MS, [])


def test_naira_owed_counts_unpaid_buy_orders_and_clears_when_they_close():
    bot = make_bot()
    bot.bybit = FakeBybit(["b1", "b2"], {"b1": buy_order("b1", STATUS_WAITING_BUYER_PAY, "300000"),
                                         "b2": buy_order("b2", STATUS_WAITING_BUYER_PAY, "200000")})
    bot.poll_orders()
    assert bot.db.naira_owed() == Decimal("500000")
    # b2 is cancelled and drops off the pending list: it must stop counting.
    from p2pbot.bybit_client import STATUS_CANCELLED
    bot.bybit = FakeBybit(["b1"], {"b1": buy_order("b1", STATUS_WAITING_BUYER_PAY, "300000"),
                                   "b2": buy_order("b2", STATUS_CANCELLED, "200000")})
    bot.poll_orders()
    assert bot.db.naira_owed() == Decimal("300000")

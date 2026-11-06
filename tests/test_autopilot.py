"""Automatic release, payment and refill, against fake Bybit and Nomba."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from p2pbot.bot import Bot
from p2pbot.bybit_client import (SIDE_BUY, SIDE_SELL, STATUS_CANCELLED, STATUS_WAITING_BUYER_PAY,
                                 STATUS_WAITING_SELLER_RELEASE, P2POrder, PaymentTerm)
from p2pbot.config import AutoSettings, RefillSettings
from p2pbot.db import Database
from p2pbot.nomba_client import NombaError
from p2pbot.webhook import compute_signature

from .test_bot import FakeTelegram, settings

D = Decimal
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "nomba-webhook.json").read_text())
KEY = "test-signing-key"
ORDER_MS = 1_759_142_000_000


class FakeBybit:
    def __init__(self):
        self.released, self.marked_paid, self.withdrawals = [], [], []
        self.fail_release = False

    def release_assets(self, order_id):
        if self.fail_release:
            raise requests.ConnectionError("timeout")
        self.released.append(order_id)

    def mark_paid(self, order_id, payment_type, payment_id):
        self.marked_paid.append((order_id, payment_type, payment_id))

    def withdraw(self, coin, chain, address, amount):
        self.withdrawals.append((coin, chain, address, amount))
        return "w1"


class FakeNomba:
    def __init__(self, balance="5000000"):
        self.balance_value = D(balance)
        self.transfers = []
        self.transfer_status = "SUCCESS"
        self.transfer_error = None
        self.requery_status = "SUCCESS"
        self.wallets = [{"currency": "NGN", "availableBalance": 0}, {"currency": "USDT", "availableBalance": 0}]
        self.rate = D("1590")
        self.exchanges = []

    def get_transaction(self, ref):
        return {"amount": "10", "status": "SUCCESS"}

    def balance(self):
        return self.balance_value

    def banks(self):
        return [{"code": "058", "name": "Guaranty Trust Bank"}]

    def lookup_account_name(self, account_no, code):
        return {"0123456789": "ADEBAYO TUNDE"}.get(account_no, "SOMEONE ELSE")

    def transfer_to_bank(self, amount, account_no, account_name, bank_code, reference, narration):
        self.transfers.append((amount, account_no, account_name, bank_code, reference))
        if self.transfer_error:
            raise self.transfer_error
        return {"status": self.transfer_status}

    def get_transaction_by_reference(self, ref):
        return {"status": self.requery_status}

    def currency_wallets(self):
        return self.wallets

    def quote_conversion(self, amount, currency, to_currency):
        return {"toAmount": str(amount * self.rate), "exchangeRateId": "rate-1"}

    def exchange(self, amount, from_currency, to_currency, rate_id, name, country, key):
        self.exchanges.append((amount, rate_id, name, key))
        return {"status": "PROCESSING"}


def live_bot(auto=None, refill=None, nomba=None, withdrawer=None):
    s = settings(monitor_only=False,
                 auto=auto or AutoSettings(auto_release=True, auto_pay=True),
                 refill=refill or RefillSettings())
    bot = Bot(s, bybit=FakeBybit(), nomba=nomba or FakeNomba(), db=Database(":memory:"),
              telegram=FakeTelegram(), withdrawer=withdrawer)
    bot.webhook_tolerance = None
    return bot


def deliver(bot):
    ts = "2025-09-29T10:51:44Z"
    hdrs = {"nomba-signature": compute_signature(FIXTURE, ts, KEY), "nomba-timestamp": ts}
    assert bot.on_webhook(json.dumps(FIXTURE).encode(), hdrs) == 200


def sell(status=STATUS_WAITING_SELLER_RELEASE, buyer="John Doe", amount="10", oid="s1"):
    return P2POrder(oid, SIDE_SELL, status, "USDT", "NGN", D("1600"), D("0.00625"), D(amount),
                    buyer, "Me", "nick", ORDER_MS, [])


def buy(status=STATUS_WAITING_BUYER_PAY, amount="632000", account="0123456789", oid="b1"):
    return P2POrder(oid, SIDE_BUY, status, "USDT", "NGN", D("1580"), D("400"), D(amount), "Me",
                    "Tunde Adebayo", "nick", ORDER_MS,
                    [PaymentTerm("Tunde Adebayo", account, "GTBank Guaranty Trust", id="p9", payment_type="14")])


def seed_history(bot, name, completed=1):
    for i in range(completed):
        bot.db.upsert_order(f"old-{name}-{i}", SIDE_BUY, 50, D("1"), D("1"), D("1"), name, 0)


# --- SELL: automatic release ---

def test_confirmed_payment_is_released_once():
    bot = live_bot()
    seed_history(bot, "John Doe")
    deliver(bot)
    bot.process_order(sell())
    bot.process_order(sell())
    assert bot.bybit.released == ["s1"]
    assert any("🚀" in m and "released" in m for m in bot.tg.sent)


def test_third_party_payment_is_never_auto_released():
    bot = live_bot()
    deliver(bot)
    bot.process_order(sell(buyer="Mary Jane"))
    assert bot.bybit.released == []


def test_no_payment_no_release():
    bot = live_bot()
    bot.process_order(sell())
    assert bot.bybit.released == []


def test_monitor_only_never_releases():
    bot = live_bot()
    bot.s = bot.s.__class__(**{**bot.s.__dict__, "monitor_only": True})
    bot.autopilot.monitor_only = True
    deliver(bot)
    bot.process_order(sell())
    assert bot.bybit.released == []


def test_release_switch_off_holds_order_for_approval_then_releases_on_command():
    bot = live_bot(auto=AutoSettings(auto_release=False))
    deliver(bot)
    bot.process_order(sell())
    assert bot.bybit.released == []
    assert any("/release s1" in m for m in bot.tg.sent)
    assert "releasing" in bot.approve_release("s1")
    bot.process_order(sell())
    assert bot.bybit.released == ["s1"]


def test_first_time_buyer_over_new_counterparty_cap_waits():
    bot = live_bot(auto=AutoSettings(auto_release=True, new_counterparty_max=D("5")))
    deliver(bot)
    bot.process_order(sell())  # ₦10 > ₦5 cap, no history with John Doe
    assert bot.bybit.released == []
    assert any("first trade" in m for m in bot.tg.sent)


def test_daily_limit_holds_orders():
    bot = live_bot(auto=AutoSettings(auto_release=True, daily_limit=D("15")))
    seed_history(bot, "John Doe")
    bot.db.record_auto("pay", D("10"))
    deliver(bot)
    bot.process_order(sell())
    assert bot.bybit.released == []
    assert any("limit" in m for m in bot.tg.sent)


def test_stop_halts_everything():
    bot = live_bot()
    seed_history(bot, "John Doe")
    bot.stop_autopilot()
    deliver(bot)
    bot.process_order(sell())
    assert bot.bybit.released == []
    bot.start_autopilot()
    bot.db.set_last_notice("s1", "")
    bot.process_order(sell())
    # The credit is already claimed by s1, so only an approval releases it now.
    bot.approve_release("s1")
    bot.process_order(sell())
    assert bot.bybit.released == ["s1"]


def test_failed_release_is_reported():
    bot = live_bot()
    seed_history(bot, "John Doe")
    bot.bybit.fail_release = True
    deliver(bot)
    bot.process_order(sell())
    assert any("release FAILED" in m for m in bot.tg.sent)


# --- BUY: automatic payment ---

def test_pays_seller_and_marks_order_paid():
    bot = live_bot(auto=AutoSettings(auto_pay=True, max_order=D("1000000"), new_counterparty_max=D("1000000")))
    bot.process_order(buy())
    assert bot.nomba.transfers == [(D("632000"), "0123456789", "ADEBAYO TUNDE", "058", "bybit-p2p-b1")]
    assert bot.bybit.marked_paid == [("b1", "14", "p9")]
    assert any("💸" in m for m in bot.tg.sent)


def test_seller_is_never_paid_twice():
    bot = live_bot(auto=AutoSettings(auto_pay=True, max_order=D("1000000"), new_counterparty_max=D("1000000")))
    for _ in range(3):
        bot.process_order(buy())
    assert len(bot.nomba.transfers) == 1
    assert len(bot.bybit.marked_paid) == 1


def test_name_mismatch_is_not_paid():
    bot = live_bot(auto=AutoSettings(auto_pay=True, max_order=D("1000000"), new_counterparty_max=D("1000000")))
    bot.process_order(buy(account="9999999999"))
    assert bot.nomba.transfers == []
    assert any("doesn't match" in m and "/pay b1" in m for m in bot.tg.sent)


def test_not_enough_naira_is_not_paid():
    bot = live_bot(auto=AutoSettings(auto_pay=True, max_order=D("1000000"), new_counterparty_max=D("1000000")),
                   nomba=FakeNomba(balance="100000"))
    bot.process_order(buy())
    assert bot.nomba.transfers == []
    assert any("only ₦100000 in Nomba" in m for m in bot.tg.sent)


def test_over_limit_waits_for_pay_command():
    bot = live_bot(auto=AutoSettings(auto_pay=True, max_order=D("500000")))
    bot.process_order(buy())
    assert bot.nomba.transfers == []
    bot.approve_pay("b1")
    bot.process_order(buy())
    assert len(bot.nomba.transfers) == 1


def test_unclear_transfer_is_checked_not_resent():
    nomba = FakeNomba()
    nomba.transfer_error = requests.ConnectionError("connection reset")
    bot = live_bot(auto=AutoSettings(auto_pay=True, max_order=D("1000000"), new_counterparty_max=D("1000000")),
                   nomba=nomba)
    bot.process_order(buy())
    assert any("will NOT be sent again" in m for m in bot.tg.sent)
    nomba.transfer_error = None
    bot.process_order(buy())  # next poll: asks Nomba by reference, finds SUCCESS
    assert len(nomba.transfers) == 1
    assert bot.bybit.marked_paid == [("b1", "14", "p9")]


def test_pending_transfer_marks_paid_only_after_success():
    nomba = FakeNomba()
    nomba.transfer_status = "PROCESSING"
    nomba.requery_status = "PROCESSING"
    bot = live_bot(auto=AutoSettings(auto_pay=True, max_order=D("1000000"), new_counterparty_max=D("1000000")),
                   nomba=nomba)
    bot.process_order(buy())
    assert bot.bybit.marked_paid == []
    nomba.requery_status = "SUCCESS"
    bot.process_order(buy())
    assert bot.bybit.marked_paid == [("b1", "14", "p9")]


def test_cancelled_after_payment_raises_alarm():
    bot = live_bot(auto=AutoSettings(auto_pay=True, max_order=D("1000000"), new_counterparty_max=D("1000000")))
    bot.process_order(buy())
    bot.process_order(buy(status=STATUS_CANCELLED))
    assert any("🚨" in m and "CANCELLED" in m for m in bot.tg.sent)


# --- Refill: USDT -> NGN in Nomba ---

REFILL = RefillSettings(auto_refill=True, target_naira=D("2000000"), min_rate=D("1550"), min_usdt=D("20"),
                        max_usdt_per_day=D("1000"), check_seconds=0, business_name="My Ventures Ltd",
                        nomba_usdt_address="TXyz", nomba_usdt_chain="TRX")


def test_refill_converts_usdt_already_in_nomba():
    nomba = FakeNomba()
    nomba.wallets[1]["availableBalance"] = 800
    bot = live_bot(refill=REFILL, nomba=nomba)
    message = bot.refiller.tick(D("300000"), D("500"))  # free naira 300k < 500k alert level
    amount, rate_id, name, _key = nomba.exchanges[0]
    # needs ₦1.7m at ₦1590 ≈ 1069 USDT, but only 800 in Nomba and 1000/day cap
    assert amount == D("800")
    assert rate_id == "rate-1" and name == "My Ventures Ltd"
    assert "converted 800" in message


def test_refill_respects_minimum_rate():
    nomba = FakeNomba()
    nomba.wallets[1]["availableBalance"] = 800
    nomba.rate = D("1500")
    bot = live_bot(refill=REFILL, nomba=nomba)
    message = bot.refiller.tick(D("300000"), D("500"))
    assert nomba.exchanges == []
    assert "below your REFILL_MIN_RATE" in message


def test_refill_daily_cap():
    nomba = FakeNomba()
    nomba.wallets[1]["availableBalance"] = 800
    bot = live_bot(refill=REFILL, nomba=nomba)
    bot.db.record_auto("refill_usdt", D("990"))
    message = bot.refiller.tick(D("300000"), D("500"))
    assert nomba.exchanges == []
    assert "limit is used up" in message


def test_refill_moves_usdt_from_bybit_when_nomba_is_empty():
    withdrawer = FakeBybit()
    bot = live_bot(refill=REFILL, withdrawer=withdrawer)
    bot.refiller.tick(D("300000"), D("1000"))
    coin, chain, address, amount = withdrawer.withdrawals[0]
    assert (coin, chain, address) == ("USDT", "TRX", "TXyz")
    assert amount == D("700")  # 1000 on Bybit minus the 300 USDT floor
    bot.refiller.tick(D("300000"), D("1000"))
    assert len(withdrawer.withdrawals) == 1  # waits for the first one to land


def test_refill_without_withdraw_key_asks_owner():
    bot = live_bot(refill=REFILL)
    message = bot.refiller.tick(D("300000"), D("1000"))
    assert "Send USDT from Bybit to your Nomba USDT address" in message


def test_refill_does_nothing_when_naira_is_fine():
    nomba = FakeNomba()
    nomba.wallets[1]["availableBalance"] = 800
    bot = live_bot(refill=REFILL, nomba=nomba)
    assert bot.refiller.tick(D("3000000"), D("500")) == ""
    assert nomba.exchanges == []


def test_refill_off_in_monitor_only():
    nomba = FakeNomba()
    nomba.wallets[1]["availableBalance"] = 800
    bot = live_bot(refill=REFILL, nomba=nomba)
    bot.refiller.monitor_only = True
    assert "MONITOR_ONLY" in bot.refiller.tick(D("300000"), D("500"))
    assert nomba.exchanges == []


def test_withdraw_is_blocked_in_monitor_only():
    from p2pbot.bybit_client import BybitError, BybitP2PClient
    with pytest.raises(BybitError):
        BybitP2PClient("k", "s", "https://x").withdraw("USDT", "TRX", "addr", D("1"))
    with pytest.raises(NombaError):
        from p2pbot.nomba_client import NombaClient
        NombaClient("c", "s", "a", "https://x").exchange(D("1"), "USDT", "NGN", "r", "n", "NG", "k")

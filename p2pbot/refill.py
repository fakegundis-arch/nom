"""Refill naira by converting USDT to NGN inside Nomba.

When free naira (Nomba balance minus naira owed on open BUY orders) drops
below NAIRA_LOW_THRESHOLD:
  1. If your Nomba USDT wallet has USDT: get a locked quote, check the rate
     is at least REFILL_MIN_RATE, and convert enough to get back to
     REFILL_TARGET_NAIRA.
  2. If it doesn't, and a withdraw-only Bybit key is set: send USDT from
     Bybit's Funding account to your Nomba USDT address (whitelisted in
     Bybit), never below USDT_LOW_THRESHOLD, then convert once it lands.
  3. Otherwise: tell you to move USDT yourself.

Needs MONITOR_ONLY=false and AUTO_REFILL=true. Daily cap: REFILL_MAX_USDT_PER_DAY.
"""

import logging
import time
from decimal import ROUND_DOWN, Decimal

import requests

from .bybit_client import BybitError, BybitP2PClient
from .config import RefillSettings
from .db import Database
from .nomba_client import NombaClient, NombaError

log = logging.getLogger(__name__)

# After a withdrawal, wait this long for it to reach Nomba before sending more.
WITHDRAWAL_WAIT_SECONDS = 2 * 60 * 60


def usdt_wallet(wallets: list[dict]) -> dict | None:
    return next((w for w in wallets if str(w.get("currency", "")).upper() == "USDT"), None)


class Refiller:
    def __init__(self, cfg: RefillSettings, monitor_only: bool, nomba: NombaClient, db: Database,
                 telegram, naira_low: Decimal, usdt_floor: Decimal,
                 withdrawer: BybitP2PClient | None = None):
        self.cfg = cfg
        self.monitor_only = monitor_only
        self.nomba = nomba
        self.db = db
        self.tg = telegram
        self.naira_low = naira_low
        self.usdt_floor = usdt_floor
        self.withdrawer = withdrawer
        self._last_run = 0.0

    def _say_once(self, key: str, text: str) -> None:
        if self.db.get_flag(f"refill:said:{key}") != text:
            self.db.set_flag(f"refill:said:{key}", text)
            self.tg.send(text)

    def enabled(self) -> tuple[bool, str]:
        if not self.cfg.auto_refill:
            return False, "AUTO_REFILL is off"
        if self.monitor_only:
            return False, "MONITOR_ONLY is on"
        if self.cfg.min_rate <= 0:
            return False, "set REFILL_MIN_RATE (lowest naira per USDT you accept)"
        if not self.cfg.business_name:
            return False, "set BUSINESS_NAME (your Nomba business name)"
        if self.db.get_flag("autopilot:stopped") == "1":
            return False, "autopilot stopped"
        return True, ""

    def tick(self, naira_balance: Decimal | None, usdt_on_bybit: Decimal | None, force: bool = False) -> str:
        if not force and time.time() - self._last_run < self.cfg.check_seconds:
            return ""
        self._last_run = time.time()
        ok, why = self.enabled()
        if not ok:
            return f"Refill off: {why}."
        if naira_balance is None:
            return "Waiting for the first balance check."
        free = naira_balance - self.db.naira_owed()
        if free >= self.naira_low and not force:
            return ""
        try:
            return self._refill(free, usdt_on_bybit)
        except (NombaError, BybitError, requests.RequestException, KeyError, ValueError) as exc:
            log.warning("Refill failed: %s", exc)
            self.db.log("refill_error", repr(exc))
            msg = f"❗ Naira refill failed: {exc}"
            self._say_once("error", msg)
            return msg

    def _refill(self, free: Decimal, usdt_on_bybit: Decimal | None) -> str:
        need_naira = self.cfg.target_naira - free
        if need_naira <= 0:
            return "Naira is already at the refill target."
        wallet = usdt_wallet(self.nomba.currency_wallets())
        if wallet is None:
            msg = "⚠️ No USDT wallet on your Nomba account, so the bot can't convert USDT to naira. Ask Nomba to enable it."
            self._say_once("no_wallet", msg)
            return msg

        used_today = self.db.auto_total_today(("refill_usdt",))
        room = self.cfg.max_usdt_per_day - used_today
        if room < self.cfg.min_usdt:
            msg = f"⚠️ Naira low, but today's {self.cfg.max_usdt_per_day} USDT refill limit is used up."
            self._say_once("cap", msg)
            return msg

        in_nomba = Decimal(str(wallet.get("availableBalance", wallet.get("balance", 0))))
        if in_nomba >= self.cfg.min_usdt:
            return self._convert(in_nomba, need_naira, room)
        return self._move_from_bybit(usdt_on_bybit, need_naira, room)

    def _convert(self, in_nomba: Decimal, need_naira: Decimal, room: Decimal) -> str:
        # Price the conversion with a 1-USDT quote, then size it.
        probe = self.nomba.quote_conversion(Decimal("1"), "USDT", "NGN")
        rate = Decimal(str(probe["toAmount"]))
        if rate < self.cfg.min_rate:
            msg = (f"⚠️ Naira low, but Nomba offers ₦{rate}/USDT, below your REFILL_MIN_RATE ₦{self.cfg.min_rate}. "
                   f"Not converting; your SELL ad will bring naira in.")
            self._say_once("rate", msg)
            return msg
        usdt = min(in_nomba, room, (need_naira / rate).quantize(Decimal("0.01"), ROUND_DOWN) + 1)
        quote = self.nomba.quote_conversion(usdt, "USDT", "NGN")
        to_amount = Decimal(str(quote["toAmount"]))
        if to_amount / usdt < self.cfg.min_rate:
            return f"Quote moved below your minimum rate (₦{(to_amount / usdt):.2f}); trying again later."
        key = f"refill-{int(time.time())}"
        # Count it before calling Nomba: if the result is unclear, the daily cap
        # still stops the bot from converting again and again.
        self.db.record_auto("refill_usdt", usdt)
        result = self.nomba.exchange(usdt, "USDT", "NGN", str(quote["exchangeRateId"]),
                                     self.cfg.business_name, self.cfg.source_country, key)
        self.db.log("refill", f"{usdt} USDT -> ₦{to_amount} status={result.get('status')} key={key}")
        msg = f"🔄 Refill: converted {usdt} USDT to about ₦{to_amount} in Nomba ({result.get('status', 'sent')})."
        self.tg.send(msg)
        return msg

    def _move_from_bybit(self, usdt_on_bybit: Decimal | None, need_naira: Decimal, room: Decimal) -> str:
        if self.withdrawer is None or not self.cfg.nomba_usdt_address:
            msg = ("⚠️ Naira low and no USDT in Nomba. Send USDT from Bybit to your Nomba USDT address; "
                   "the bot will convert it automatically.")
            self._say_once("manual_move", msg)
            return msg
        sent_at = float(self.db.get_flag("refill:withdrawal_at") or 0)
        if time.time() - sent_at < WITHDRAWAL_WAIT_SECONDS:
            return "Waiting for the last USDT withdrawal to reach Nomba."
        if usdt_on_bybit is None:
            return "Waiting for the Bybit balance."
        rate_guess = self.cfg.min_rate
        spare = usdt_on_bybit - self.usdt_floor
        amount = min(spare, room, (need_naira / rate_guess).quantize(Decimal("1"), ROUND_DOWN) + 1)
        if amount < self.cfg.min_usdt:
            msg = "⚠️ Naira low, and Bybit has no USDT to spare above USDT_LOW_THRESHOLD. Top up naira by hand."
            self._say_once("no_usdt", msg)
            return msg
        withdrawal_id = self.withdrawer.withdraw("USDT", self.cfg.nomba_usdt_chain, self.cfg.nomba_usdt_address, amount)
        self.db.set_flag("refill:withdrawal_at", str(time.time()))
        self.db.log("refill_withdraw", f"{amount} USDT to Nomba, Bybit id {withdrawal_id}")
        msg = f"📤 Refill: sent {amount} USDT from Bybit to your Nomba USDT wallet. It'll be converted when it lands."
        self.tg.send(msg)
        return msg

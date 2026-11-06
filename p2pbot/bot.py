"""The bot: watch Bybit P2P orders and Nomba credits, match them, act or alert on Telegram.

While MONITOR_ONLY is on (the default) it never releases crypto or sends
money: the clients refuse to. With MONITOR_ONLY=false, AUTO_RELEASE /
AUTO_PAY / AUTO_REFILL each switch on one kind of automatic action, inside
the limits in autopilot.py and refill.py.
"""

import logging
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from . import webhook
from .ads import AdManager
from .autopilot import Autopilot, find_bank_code
from .bybit_client import (SIDE_BUY, SIDE_SELL, STATUS_APPEALING, STATUS_CANCELLED, STATUS_FINISHED,
                           STATUS_WAITING_BUYER_PAY, STATUS_WAITING_SELLER_RELEASE, BybitError,
                           BybitP2PClient, P2POrder)
from .config import Settings, load_settings
from .db import Database
from .matcher import Decision, match_sell_order
from .nomba_client import NombaClient, NombaError
from .refill import Refiller
from .telegram import Telegram

log = logging.getLogger("p2pbot")

BALANCE_CHECK_SECONDS = 60
CONFIRMED_STATUSES = {"SUCCESS", "PAYMENT_SUCCESSFUL"}

__all__ = ["Bot", "find_bank_code", "main"]


class Bot:
    def __init__(self, settings: Settings, bybit: BybitP2PClient, nomba: NombaClient,
                 db: Database, telegram: Telegram, withdrawer: BybitP2PClient | None = None):
        self.s = settings
        self.bybit = bybit
        self.nomba = nomba
        self.db = db
        self.tg = telegram
        self.naira_balance: Decimal | None = None
        self.usdt_balance: Decimal | None = None
        self._last_balance_check = 0.0
        self.webhook_tolerance: float | None = webhook.TOLERANCE_SECONDS
        self.ads = AdManager(settings.ads, bybit, db, telegram,
                             settings.naira_low_threshold, settings.usdt_low_threshold)
        self.autopilot = Autopilot(settings.auto, settings.monitor_only, bybit, nomba, db)
        self.refiller = Refiller(settings.refill, settings.monitor_only, nomba, db, telegram,
                                 settings.naira_low_threshold, settings.usdt_low_threshold, withdrawer)

    # --- alerts ---

    def notify_once(self, order_id: str, notice: str, text: str) -> None:
        """Send `text` unless this order already got this exact notice."""
        if self.db.last_notice(order_id) == notice:
            return
        self.db.set_last_notice(order_id, notice)
        self.db.log("alert", text, order_id)
        self.tg.send(text)

    # --- incoming bank credits (called from the webhook thread) ---

    def on_webhook(self, raw_body: bytes, headers: dict[str, str]) -> int:
        try:
            event = webhook.verify(raw_body, headers, self.s.nomba_webhook_secret, self.webhook_tolerance)
        except webhook.WebhookError as exc:
            log.warning("Rejected webhook: %s", exc)
            self.db.log("webhook_rejected", str(exc))
            return 401
        credit = webhook.parse_incoming_credit(event)
        if credit is None:
            return 200
        # The signature doesn't cover the amount: ask Nomba what really arrived.
        try:
            txn = self.nomba.get_transaction(credit.transaction_id)
        except (NombaError, requests.RequestException) as exc:
            self.db.log("credit_unconfirmed", f"{credit.transaction_id}: {exc}")
            self.tg.send(f"⚠️ Webhook said ₦{credit.amount_hint} arrived from {credit.sender_name}, "
                         f"but Nomba could not confirm it ({exc}). Check the Nomba app.")
            return 500  # Nomba will retry the webhook
        status = str(txn.get("status", "")).upper()
        if status not in CONFIRMED_STATUSES:
            self.db.log("credit_not_successful", f"{credit.transaction_id} status={status}")
            return 200
        amount = Decimal(str(txn.get("amount", "0")))
        if amount != credit.amount_hint:
            self.db.log("credit_amount_mismatch",
                        f"{credit.transaction_id} webhook={credit.amount_hint} api={amount}")
        if self.db.add_credit(credit.transaction_id, amount, credit.sender_name, credit.sender_bank,
                              credit.received_at, event):
            self.db.log("credit", f"₦{amount} from {credit.sender_name} ({credit.sender_bank})")
            self.tg.send(f"💰 ₦{amount} received from {credit.sender_name} ({credit.sender_bank}).")
        return 200

    # --- orders ---

    def process_order(self, order: P2POrder) -> None:
        self.db.upsert_order(order.order_id, order.side, order.status, order.amount, order.quantity,
                             order.price, order.counterparty_real_name, order.created_ms)
        oid = order.order_id
        who = order.counterparty_real_name or order.counterparty_nickname
        summary = f"{order.quantity} {order.token} @ ₦{order.price} = ₦{order.amount}"

        if order.status == STATUS_APPEALING:
            self.notify_once(oid, "appeal", f"🚨 Order {oid} is under APPEAL ({summary}, {who}). Handle it in Bybit.")
        elif order.status in (STATUS_CANCELLED, STATUS_FINISHED):
            label = "cancelled" if order.status == STATUS_CANCELLED else "completed"
            payout = self.db.payout(oid) if order.side == SIDE_BUY else None
            if order.status == STATUS_CANCELLED and payout and payout["status"] not in ("REFUND", "CANCELLED"):
                self.notify_once(oid, "cancelled_after_pay",
                                 f"🚨 BUY {oid} was CANCELLED but the bot sent ₦{payout['amount']} to "
                                 f"{payout['account_name']} (status {payout['status']}). Contact the seller / "
                                 f"Bybit support now to get the USDT or the money back.")
            else:
                self.notify_once(oid, label, f"Order {oid} {label}.")
        elif order.side == SIDE_SELL:
            self._process_sell(order, who, summary)
        elif order.side == SIDE_BUY:
            self._process_buy(order, who, summary)

    def _process_sell(self, order: P2POrder, buyer: str, summary: str) -> None:
        oid = order.order_id
        if order.status == STATUS_WAITING_BUYER_PAY:
            self.notify_once(oid, "sell_new", f"🟡 SELL {oid}: {summary} to {buyer}. Waiting for buyer to pay.")
            return
        if order.status != STATUS_WAITING_SELLER_RELEASE:
            return
        if self.autopilot.approved("release", oid):
            message = self.autopilot.release(order, "Approved by you")
            if message:
                self.notify_once(oid, "release_approved", message)
            return
        if (self.db.last_notice(oid) or "").startswith("paid:"):
            return  # already matched; the credit is claimed by this order
        window = self.s.payment_window_minutes * 60
        created = order.created_ms / 1000
        result = match_sell_order(order.amount, order.buyer_real_name, created, window,
                                  self.db.credits_since(created - 600))
        if result.decision == Decision.PAID and result.credit:
            if not self.db.mark_credit_used(result.credit.txn_id, oid):
                return  # another order claimed it a moment ago; re-check next poll
            message = self.autopilot.release(order, result.reason)
            if message:
                self.notify_once(oid, f"paid:{result.credit.txn_id}", message)
        elif result.decision == Decision.REVIEW and result.credit:
            self.notify_once(oid, f"review:{result.credit.txn_id}",
                             f"⚠️ SELL {oid}: {result.reason}")
        else:
            self.notify_once(oid, "sell_claimed_unpaid",
                             f"⏳ SELL {oid}: buyer {buyer} marked paid, but no matching ₦{order.amount} "
                             f"credit yet. Do NOT release.")

    def _process_buy(self, order: P2POrder, seller: str, summary: str) -> None:
        oid = order.order_id
        if order.status == STATUS_WAITING_SELLER_RELEASE:
            self.notify_once(oid, "buy_marked_paid", f"🔵 BUY {oid}: marked paid, waiting for {seller} to release.")
            return
        if order.status != STATUS_WAITING_BUYER_PAY:
            return
        outcome = self.autopilot.handle_buy(order)
        if outcome:
            self.notify_once(oid, *outcome)

    def poll_orders(self) -> None:
        pending = self.bybit.pending_order_ids()
        # Orders that dropped off the pending list finished or were cancelled:
        # fetch them once more so they stop counting as naira we owe.
        stale = [oid for oid in self.db.open_order_ids() if oid not in pending]
        for order_id in pending:
            self.process_order(self.bybit.order_detail(order_id))
        for order_id in stale:
            try:
                self.process_order(self.bybit.order_detail(order_id))
            except (BybitError, requests.RequestException) as exc:
                log.warning("Could not refresh order %s: %s", order_id, exc)

    # --- balances ---

    def check_balances(self) -> None:
        if time.time() - self._last_balance_check < BALANCE_CHECK_SECONDS:
            return
        self._last_balance_check = time.time()
        try:
            self.naira_balance = self.nomba.balance()
            self._low_alert("naira", self.naira_balance, self.s.naira_low_threshold, "₦")
        except (NombaError, requests.RequestException) as exc:
            log.warning("Nomba balance failed: %s", exc)
        try:
            self.usdt_balance = self.bybit.usdt_funding_balance()
            self._low_alert("usdt", self.usdt_balance, self.s.usdt_low_threshold, "USDT ")
        except (BybitError, requests.RequestException) as exc:
            log.warning("Bybit balance failed: %s", exc)

    def _low_alert(self, name: str, value: Decimal, threshold: Decimal, unit: str) -> None:
        key = f"low:{name}"
        is_low = value < threshold
        was_low = self.db.get_flag(key) == "1"
        if is_low and not was_low:
            self.tg.send(f"📉 {name.upper()} low: {unit}{value} (alert level {unit}{threshold}).")
        elif was_low and not is_low:
            self.tg.send(f"📈 {name.upper()} back above alert level: {unit}{value}.")
        self.db.set_flag(key, "1" if is_low else "0")

    # --- telegram commands ---

    def status_text(self, _args: str = "") -> str:
        counts = self.db.order_counts()
        naira = "unknown" if self.naira_balance is None else f"₦{self.naira_balance}"
        usdt = "unknown" if self.usdt_balance is None else f"{self.usdt_balance} USDT"
        a = self.s.auto
        if self.s.monitor_only:
            mode = "MONITOR ONLY (no money moves)"
        elif self.autopilot.stopped:
            mode = "LIVE, autopilot STOPPED (/go to resume)"
        else:
            on = [n for n, v in (("release", a.auto_release), ("pay", a.auto_pay),
                                 ("refill", self.s.refill.auto_refill)) if v]
            mode = f"LIVE, automatic: {', '.join(on) or 'nothing'}"
        today = self.db.auto_total_today(("release", "pay"))
        return (f"Mode: {mode}\nNomba: {naira}\nBybit funding: {usdt}\n"
                f"Handled automatically today: ₦{today} of ₦{a.daily_limit}\n"
                f"Naira owed on open BUY orders: ₦{self.db.naira_owed()}\n"
                f"Orders seen by status: {counts or 'none yet'}")

    def stop_autopilot(self, _args: str = "") -> str:
        self.autopilot.stop()
        return "🛑 Autopilot STOPPED: no automatic releases, payments or refills. /go to resume."

    def start_autopilot(self, _args: str = "") -> str:
        self.autopilot.start()
        return "▶️ Autopilot running again."

    def approve_release(self, args: str) -> str:
        order_id = args.strip()
        if not order_id:
            return "Usage: /release ORDER_ID"
        if self.s.monitor_only:
            return "MONITOR_ONLY is on, so the bot can't release. Release it in Bybit."
        self.autopilot.approve("release", order_id)
        return f"OK: releasing SELL {order_id} on the next check (a few seconds) if it's waiting for release."

    def approve_pay(self, args: str) -> str:
        order_id = args.strip()
        if not order_id:
            return "Usage: /pay ORDER_ID"
        if self.s.monitor_only:
            return "MONITOR_ONLY is on, so the bot can't pay. Pay by hand."
        if self.db.payout(order_id):
            return f"BUY {order_id} already has a payment; it won't be sent twice."
        self.autopilot.approve("pay", order_id)
        self.db.set_last_notice(order_id, "")  # let the next check report the payment
        return f"OK: paying BUY {order_id} on the next check (a few seconds) if it's still waiting for payment."

    def refill_now(self, _args: str = "") -> str:
        return self.refiller.tick(self.naira_balance, self.usdt_balance, force=True) or "Nothing to refill."

    def pause_ads(self, _args: str = "") -> str:
        self.ads.paused = True
        self.db.log("ads_paused", "paused from Telegram")
        return "⏸ Ad changes paused. Your ads stay as they are now. /resume to restart."

    def resume_ads(self, _args: str = "") -> str:
        self.ads.paused = False
        self.db.log("ads_resumed", "resumed from Telegram")
        return "▶️ Ad management resumed."

    def run_forever(self) -> None:
        self.tg.send(f"🤖 Bot started. {self.status_text()}")
        commands = {
            "/status": self.status_text,
            "/balance": self.status_text,
            "/prices": lambda _args="": self.ads.describe(),
            "/pause": self.pause_ads,
            "/resume": self.resume_ads,
            "/stop": self.stop_autopilot,
            "/go": self.start_autopilot,
            "/release": self.approve_release,
            "/pay": self.approve_pay,
            "/refill": self.refill_now,
            "/help": lambda _args="": (
                "/status: balances, mode, today's totals\n"
                "/stop, /go: halt or resume ALL automatic releases, payments and refills\n"
                "/release ID: release a SELL order the bot held back\n"
                "/pay ID: pay a BUY order the bot held back\n"
                "/refill: try a naira refill now\n"
                "/prices: ad pricing plan; /pause, /resume: ad changes"),
        }
        while True:
            try:
                self.poll_orders()
                self.check_balances()
                self.ads.tick(self.naira_balance, self.usdt_balance)
                self.refiller.tick(self.naira_balance, self.usdt_balance)
                self.tg.handle_commands(commands)
            except Exception as exc:  # keep running; tell the owner
                log.exception("Loop error")
                self.db.log("error", repr(exc))
                self.tg.send(f"❗ Bot error: {exc}")
            time.sleep(self.s.poll_seconds)


def make_webhook_handler(bot: Bot) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/webhooks/nomba":
                self.send_response(404)
                self.end_headers()
                return
            length = min(int(self.headers.get("Content-Length", 0)), 1_000_000)
            body = self.rfile.read(length)
            code = bot.on_webhook(body, dict(self.headers.items()))
            self.send_response(code)
            self.end_headers()

        def log_message(self, fmt: str, *args) -> None:
            log.info("webhook %s", fmt % args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    db = Database(settings.db_path)
    r = settings.refill
    withdrawer = (BybitP2PClient(r.bybit_withdraw_key, r.bybit_withdraw_secret, settings.bybit_base_url,
                                 monitor_only=settings.monitor_only)
                  if r.bybit_withdraw_key and r.bybit_withdraw_secret else None)
    bot = Bot(
        settings,
        BybitP2PClient(settings.bybit_api_key, settings.bybit_api_secret, settings.bybit_base_url,
                       monitor_only=settings.monitor_only),
        NombaClient(settings.nomba_client_id, settings.nomba_client_secret, settings.nomba_account_id,
                    settings.nomba_base_url, monitor_only=settings.monitor_only),
        db,
        Telegram(settings.telegram_bot_token, settings.telegram_chat_id),
        withdrawer=withdrawer,
    )
    server = ThreadingHTTPServer((settings.webhook_host, settings.webhook_port), make_webhook_handler(bot))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Webhook listener on %s:%s/webhooks/nomba", settings.webhook_host, settings.webhook_port)
    bot.run_forever()


if __name__ == "__main__":
    main()

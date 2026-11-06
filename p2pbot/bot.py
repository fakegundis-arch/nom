"""Phase 1 bot: watch Bybit P2P orders and Nomba credits, match them, alert on Telegram.

It never releases crypto or sends money: while MONITOR_ONLY is on, the
clients refuse to. You act on its alerts by hand.
"""

import logging
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from . import webhook
from .bybit_client import (SIDE_BUY, SIDE_SELL, STATUS_APPEALING, STATUS_CANCELLED, STATUS_FINISHED,
                           STATUS_WAITING_BUYER_PAY, STATUS_WAITING_SELLER_RELEASE, BybitError,
                           BybitP2PClient, P2POrder)
from .config import Settings, load_settings
from .db import Database
from .matcher import Decision, compare_names, match_sell_order, name_words
from .nomba_client import NombaClient, NombaError
from .telegram import Telegram

log = logging.getLogger("p2pbot")

BALANCE_CHECK_SECONDS = 60
CONFIRMED_STATUSES = {"SUCCESS", "PAYMENT_SUCCESSFUL"}


def find_bank_code(bank_name: str, banks: list[dict[str, str]]) -> str | None:
    """Match a bank name typed by a Bybit user ("GTBank", "Guaranty Trust") to a Nomba bank code."""
    wanted = name_words(bank_name)
    if not wanted:
        return None
    best, best_score = None, 0
    for bank in banks:
        score = len(wanted & name_words(bank.get("name", "")))
        if score > best_score:
            best, best_score = bank.get("code"), score
    return best


class Bot:
    def __init__(self, settings: Settings, bybit: BybitP2PClient, nomba: NombaClient,
                 db: Database, telegram: Telegram):
        self.s = settings
        self.bybit = bybit
        self.nomba = nomba
        self.db = db
        self.tg = telegram
        self.naira_balance: Decimal | None = None
        self.usdt_balance: Decimal | None = None
        self._last_balance_check = 0.0
        self.webhook_tolerance: float | None = webhook.TOLERANCE_SECONDS

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
        if (self.db.last_notice(oid) or "").startswith("paid:"):
            return  # already matched; the credit is claimed by this order
        window = self.s.payment_window_minutes * 60
        created = order.created_ms / 1000
        result = match_sell_order(order.amount, order.buyer_real_name, created, window,
                                  self.db.credits_since(created - 600))
        if result.decision == Decision.PAID and result.credit:
            if not self.db.mark_credit_used(result.credit.txn_id, oid):
                return  # another order claimed it a moment ago; re-check next poll
            self.notify_once(oid, f"paid:{result.credit.txn_id}",
                             f"✅ SELL {oid}: payment confirmed. {result.reason}.\n"
                             f"Phase 1: release the USDT yourself in Bybit.")
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
        lines = [f"🔵 BUY {oid}: {summary} from {seller}. You need to pay."]
        for term in order.payment_terms:
            lines.append(self._check_payee(term.account_no, term.bank_name, order.seller_real_name))
        if not order.payment_terms:
            lines.append("No bank details on the order: check Bybit.")
        lines.append("Phase 1: send the transfer yourself, then mark paid in Bybit.")
        self.notify_once(oid, "buy_new", "\n".join(lines))

    def _check_payee(self, account_no: str, bank_name: str, kyc_name: str) -> str:
        try:
            code = find_bank_code(bank_name, self.nomba.banks())
            if not code:
                return f"• {bank_name} {account_no}: bank not recognised, check by hand."
            holder = self.nomba.lookup_account_name(account_no, code)
        except (NombaError, requests.RequestException) as exc:
            return f"• {bank_name} {account_no}: name lookup failed ({exc})."
        level = compare_names(holder, kyc_name)
        verdict = "OK" if level.value in ("exact", "strong") else "MISMATCH, don't pay until checked"
        return f"• {bank_name} {account_no}: account name {holder!r} vs KYC {kyc_name!r} → {verdict}"

    def poll_orders(self) -> None:
        for order_id in self.bybit.pending_order_ids():
            self.process_order(self.bybit.order_detail(order_id))

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

    def status_text(self) -> str:
        counts = self.db.order_counts()
        naira = "unknown" if self.naira_balance is None else f"₦{self.naira_balance}"
        usdt = "unknown" if self.usdt_balance is None else f"{self.usdt_balance} USDT"
        mode = "MONITOR ONLY (no money moves)" if self.s.monitor_only else "LIVE"
        return (f"Mode: {mode}\nNomba: {naira}\nBybit funding: {usdt}\n"
                f"Orders seen by status: {counts or 'none yet'}")

    def run_forever(self) -> None:
        self.tg.send(f"🤖 Bot started. {self.status_text()}")
        commands = {
            "/status": self.status_text,
            "/balance": self.status_text,
            "/help": lambda: "/status, /balance: balances and order counts. Phase 1 only watches and alerts.",
        }
        while True:
            try:
                self.poll_orders()
                self.check_balances()
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
    bot = Bot(
        settings,
        BybitP2PClient(settings.bybit_api_key, settings.bybit_api_secret, settings.bybit_base_url,
                       monitor_only=settings.monitor_only),
        NombaClient(settings.nomba_client_id, settings.nomba_client_secret, settings.nomba_account_id,
                    settings.nomba_base_url, monitor_only=settings.monitor_only),
        db,
        Telegram(settings.telegram_bot_token, settings.telegram_chat_id),
    )
    server = ThreadingHTTPServer((settings.webhook_host, settings.webhook_port), make_webhook_handler(bot))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Webhook listener on %s:%s/webhooks/nomba", settings.webhook_host, settings.webhook_port)
    bot.run_forever()


if __name__ == "__main__":
    main()

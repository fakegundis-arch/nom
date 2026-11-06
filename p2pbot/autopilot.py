"""Automatic release (SELL side) and automatic payment (BUY side).

Nothing here runs unless MONITOR_ONLY=false AND the matching switch
(AUTO_RELEASE / AUTO_PAY) is on. Even then every order must pass:
  - under AUTO_MAX_ORDER (bigger orders wait for /release or /pay from you),
  - a lower cap for counterparties you've never completed a trade with,
  - the daily total limit,
  - the name checks (buyer's payment from their own name; seller's bank
    account in their own name).
/stop on Telegram halts all of it instantly; /go resumes.

A seller is never paid twice: each BUY order gets one payout row and one
Nomba reference before any money moves. If the result of a transfer is
unclear, the bot asks Nomba about that reference and never sends again.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

import requests

from .bybit_client import BybitError, BybitP2PClient, P2POrder, PaymentTerm
from .config import AutoSettings
from .db import Database
from .matcher import NameMatch, compare_names, name_words
from .nomba_client import NombaClient, NombaError

log = logging.getLogger(__name__)

DONE = {"SUCCESS", "PAYMENT_SUCCESSFUL"}
FAILED = {"REFUND", "CANCELLED"}
GOOD_NAMES = (NameMatch.EXACT, NameMatch.STRONG)


@dataclass
class Payee:
    term: PaymentTerm
    bank_code: str
    holder: str        # account name according to the bank (via Nomba)
    match: NameMatch   # holder vs the seller's Bybit KYC name


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


def payout_reference(order_id: str) -> str:
    return f"bybit-p2p-{order_id}"


class Autopilot:
    def __init__(self, cfg: AutoSettings, monitor_only: bool, bybit: BybitP2PClient,
                 nomba: NombaClient, db: Database):
        self.cfg = cfg
        self.monitor_only = monitor_only
        self.bybit = bybit
        self.nomba = nomba
        self.db = db

    # --- switches and approvals ---

    @property
    def stopped(self) -> bool:
        return self.db.get_flag("autopilot:stopped") == "1"

    def stop(self) -> None:
        self.db.set_flag("autopilot:stopped", "1")
        self.db.log("autopilot", "stopped from Telegram")

    def start(self) -> None:
        self.db.set_flag("autopilot:stopped", "0")
        self.db.log("autopilot", "started from Telegram")

    def approve(self, kind: str, order_id: str) -> None:
        self.db.set_flag(f"approve:{kind}:{order_id}", "1")
        self.db.log("approval", f"{kind} approved by owner", order_id)

    def approved(self, kind: str, order_id: str) -> bool:
        return self.db.get_flag(f"approve:{kind}:{order_id}") == "1"

    def allowed(self, kind: str, order: P2POrder) -> tuple[bool, str]:
        if self.monitor_only:
            return False, "MONITOR_ONLY is on"
        if self.stopped:
            return False, "autopilot stopped (/go to resume)"
        if self.approved(kind, order.order_id):
            return True, "approved by you"
        if not (self.cfg.auto_release if kind == "release" else self.cfg.auto_pay):
            return False, f"AUTO_{kind.upper()} is off"
        if order.amount > self.cfg.max_order:
            return False, f"over the ₦{self.cfg.max_order} automatic limit"
        if (self.db.completed_trades_with(order.counterparty_real_name) == 0
                and order.amount > self.cfg.new_counterparty_max):
            return False, f"first trade with this person and over ₦{self.cfg.new_counterparty_max}"
        if self.db.auto_total_today(("release", "pay")) + order.amount > self.cfg.daily_limit:
            return False, f"today's ₦{self.cfg.daily_limit} automatic limit would be passed"
        return True, ""

    # --- SELL side ---

    def release(self, order: P2POrder, why: str) -> str:
        """Release USDT for a SELL order whose payment is confirmed. Returns the message to send."""
        oid = order.order_id
        ok, reason = self.allowed("release", order)
        if not ok:
            return (f"✅ SELL {oid}: payment confirmed. {why}.\n"
                    f"Not released automatically ({reason}). Release in Bybit or send /release {oid}")
        if self.db.get_flag(f"released:{oid}") == "1":
            return ""
        try:
            self.bybit.release_assets(oid)
        except (BybitError, requests.RequestException) as exc:
            self.db.log("release_failed", repr(exc), oid)
            return f"❗ SELL {oid}: payment confirmed but release FAILED ({exc}). Release it in Bybit."
        self.db.set_flag(f"released:{oid}", "1")
        self.db.record_auto("release", order.amount, oid)
        self.db.log("released", why, oid)
        return f"🚀 SELL {oid}: released {order.quantity} {order.token} to {order.buyer_real_name}. {why}."

    # --- BUY side ---

    def resolve_payee(self, order: P2POrder) -> tuple[Payee | None, list[str]]:
        """Pick the seller's bank account whose holder name matches their KYC name."""
        notes, fallback = [], None
        for term in order.payment_terms:
            if not term.account_no:
                continue
            try:
                code = find_bank_code(term.bank_name, self.nomba.banks())
                if not code:
                    notes.append(f"• {term.bank_name} {term.account_no}: bank not recognised.")
                    continue
                holder = self.nomba.lookup_account_name(term.account_no, code)
            except (NombaError, requests.RequestException) as exc:
                notes.append(f"• {term.bank_name} {term.account_no}: name lookup failed ({exc}).")
                continue
            payee = Payee(term, code, holder, compare_names(holder, order.seller_real_name))
            ok = payee.match in GOOD_NAMES
            notes.append(f"• {term.bank_name} {term.account_no}: account name {holder!r} vs KYC "
                         f"{order.seller_real_name!r} → {'OK' if ok else 'MISMATCH'}")
            if ok:
                return payee, notes
            fallback = fallback or payee
        if not order.payment_terms:
            notes.append("No bank details on the order.")
        return fallback, notes

    def handle_buy(self, order: P2POrder) -> tuple[str, str] | None:
        """Pay a BUY order if allowed. Returns (notice_key, message) or None."""
        row = self.db.payout(order.order_id)
        if row is not None:
            return self._follow_payout(order, row)

        oid = order.order_id
        head = f"🔵 BUY {oid}: {order.quantity} {order.token} @ ₦{order.price} = ₦{order.amount} from {order.seller_real_name}."
        payee, notes = self.resolve_payee(order)
        ok, reason = self.allowed("pay", order)
        approved = self.approved("pay", oid)
        if payee is None:
            return "buy_manual", "\n".join([head, *notes, "Pay by hand and mark paid in Bybit."])
        if payee.match not in GOOD_NAMES and not approved:
            ok, reason = False, "seller's bank account name doesn't match their KYC name"
        if ok:
            try:
                available = self.nomba.balance()
            except (NombaError, requests.RequestException) as exc:
                ok, reason = False, f"couldn't read Nomba balance ({exc})"
            else:
                if available < order.amount:
                    ok, reason = False, f"only ₦{available} in Nomba"
        if not ok:
            return (f"buy_manual:{reason}",
                    "\n".join([head, *notes, f"Not paid automatically ({reason}).",
                               f"Pay by hand and mark paid in Bybit, or send /pay {oid}"]))
        return self._send(order, payee, head)

    def _send(self, order: P2POrder, payee: Payee, head: str) -> tuple[str, str] | None:
        oid = order.order_id
        ref = payout_reference(oid)
        if not self.db.claim_payout(oid, ref, order.amount, payee.term.account_no, payee.bank_code, payee.holder):
            return None  # already being paid
        self.db.record_auto("pay", order.amount, oid)
        try:
            result = self.nomba.transfer_to_bank(order.amount, payee.term.account_no, payee.holder,
                                                 payee.bank_code, ref, f"P2P order {oid}")
        except (NombaError, requests.RequestException) as exc:
            # We don't know whether money left. Never resend: ask Nomba next poll.
            self.db.set_payout_status(oid, "unknown")
            self.db.log("payout_unclear", repr(exc), oid)
            return "payout_unclear", (f"⚠️ BUY {oid}: transfer of ₦{order.amount} gave an unclear result ({exc}). "
                                      f"Checking with Nomba; it will NOT be sent again.")
        status = str(result.get("status", "")).upper() or "unknown"
        self.db.set_payout_status(oid, status)
        self.db.log("payout_sent", f"₦{order.amount} to {payee.holder} status={status}", oid)
        row = self.db.payout(oid)
        return self._follow_payout(order, row) if row else None

    def _follow_payout(self, order: P2POrder, row) -> tuple[str, str] | None:
        oid = order.order_id
        status = row["status"]
        if status not in DONE and status not in FAILED:
            try:
                txn = self.nomba.get_transaction_by_reference(row["reference"])
                status = str(txn.get("status", "")).upper() or status
                self.db.set_payout_status(oid, status)
            except (NombaError, requests.RequestException) as exc:
                return "payout_check", (f"⏳ BUY {oid}: waiting for Nomba to confirm the ₦{row['amount']} "
                                        f"transfer ({exc}). Not resending.")
        if status in FAILED:
            return f"payout_failed:{status}", (f"❌ BUY {oid}: Nomba transfer {status}. The money is back or stuck; "
                                               f"pay by hand if the order is still open.")
        if status not in DONE:
            return "payout_pending", f"⏳ BUY {oid}: transfer of ₦{row['amount']} is {status}."
        if row["marked_paid"]:
            return None
        term = next((t for t in order.payment_terms if t.account_no == row["account_no"]), None)
        try:
            if term is None:
                raise BybitError("payment method not found on the order")
            self.bybit.mark_paid(oid, term.payment_type, term.id)
        except (BybitError, requests.RequestException) as exc:
            return "mark_paid_failed", (f"❗ BUY {oid}: ₦{row['amount']} SENT to {row['account_name']}, but marking "
                                        f"the order paid failed ({exc}). Mark it paid in Bybit NOW.")
        self.db.set_payout_marked_paid(oid)
        return "paid_marked", (f"💸 BUY {oid}: sent ₦{row['amount']} to {row['account_name']} and marked the order "
                               f"paid. Waiting for the seller to release.")

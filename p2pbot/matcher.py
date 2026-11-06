"""Decide whether a bank credit pays a SELL order, and whether names match.

The rule that protects you from triangulation fraud: the person who sent the
naira must be the same person as the buyer's Bybit KYC name. Anything else
goes to a human.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

# Words that appear in bank account names but say nothing about who the person is.
_IGNORED_WORDS = {"MR", "MRS", "MS", "MISS", "DR", "CHIEF", "ALHAJI", "ALHAJA", "ENGR", "PROF"}

# A credit can arrive slightly before Bybit records the order (clock skew).
CLOCK_SKEW_SECONDS = 120


class NameMatch(Enum):
    EXACT = "exact"    # same words, any order
    STRONG = "strong"  # at least two words in common (e.g. middle name missing)
    WEAK = "weak"      # only one word in common
    NONE = "none"


class Decision(Enum):
    PAID = "paid"                  # amount, time and name all check out
    REVIEW = "review"              # money arrived but something is off: a human decides
    NOT_PAID = "not_paid"          # no credit for this order yet


@dataclass
class Credit:
    txn_id: str
    amount: Decimal
    sender_name: str
    received_at: float  # unix seconds
    used_by_order: str | None = None


@dataclass
class MatchResult:
    decision: Decision
    credit: Credit | None
    reason: str


def name_words(name: str) -> set[str]:
    # Bank names often look like "SAMPLE/JOHN DOE" or "DOE, JOHN A."
    words = re.split(r"[^A-Z]+", name.upper())
    return {w for w in words if len(w) > 1 and w not in _IGNORED_WORDS}


def compare_names(a: str, b: str) -> NameMatch:
    wa, wb = name_words(a), name_words(b)
    if not wa or not wb:
        return NameMatch.NONE
    if wa == wb:
        return NameMatch.EXACT
    common = len(wa & wb)
    if common >= 2:
        return NameMatch.STRONG
    if common == 1:
        return NameMatch.WEAK
    return NameMatch.NONE


def match_sell_order(order_amount: Decimal, buyer_name: str, order_created_at: float,
                     window_seconds: int, credits: list[Credit]) -> MatchResult:
    """Find the credit that pays this order.

    Only unused credits of the exact amount, received after the order was
    opened and inside the payment window, are considered.
    """
    earliest = order_created_at - CLOCK_SKEW_SECONDS
    latest = order_created_at + window_seconds
    candidates = sorted(
        (c for c in credits
         if c.used_by_order is None and c.amount == order_amount and earliest <= c.received_at <= latest),
        key=lambda c: c.received_at,
    )
    if not candidates:
        return MatchResult(Decision.NOT_PAID, None, "no unused credit of this exact amount in the payment window")

    for credit in candidates:
        if compare_names(credit.sender_name, buyer_name) in (NameMatch.EXACT, NameMatch.STRONG):
            return MatchResult(Decision.PAID, credit,
                               f"₦{credit.amount} from {credit.sender_name!r} matches buyer {buyer_name!r}")

    first = candidates[0]
    level = compare_names(first.sender_name, buyer_name)
    return MatchResult(
        Decision.REVIEW,
        first,
        f"₦{first.amount} arrived from {first.sender_name!r} but buyer is {buyer_name!r} "
        f"(name match: {level.value}). Possible third-party payment: do not release without checking.",
    )

"""Nomba webhook verification and parsing.

Nomba signs each webhook with HMAC-SHA256 (base64) over
    event_type:requestId:userId:walletId:transactionId:type:time:responseCode:timestamp
and sends it in `nomba-signature` (or `nomba-sig-value`) with the timestamp
in `nomba-timestamp`. This mirrors Nomba's official SDK.

The signature does NOT cover the amount, so a verified webhook is only a
hint: the bot re-fetches the transaction from Nomba before trusting it.
"""

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

TOLERANCE_SECONDS = 300
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")


class WebhookError(ValueError):
    pass


@dataclass
class IncomingCredit:
    transaction_id: str
    session_id: str
    amount_hint: Decimal  # from the webhook; not trusted until re-fetched
    sender_name: str
    sender_bank: str
    received_at: float


def _field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def signing_message(event: Mapping[str, Any], timestamp: str) -> str:
    data = event.get("data")
    if not isinstance(data, Mapping):
        raise WebhookError("no data object")
    merchant = data.get("merchant") or {}
    txn = data.get("transaction") or {}
    response_code = _field(txn.get("responseCode"))
    if response_code == "null":
        response_code = ""
    if ":" in response_code:
        raise WebhookError("responseCode contains ':'")
    return ":".join([
        _field(event.get("event_type")),
        _field(event.get("requestId")),
        _field(merchant.get("userId")),
        _field(merchant.get("walletId")),
        _field(txn.get("transactionId")),
        _field(txn.get("type")),
        _field(txn.get("time")),
        response_code,
        timestamp,
    ])


def compute_signature(event: Mapping[str, Any], timestamp: str, key: str) -> str:
    digest = hmac.new(key.encode(), signing_message(event, timestamp).encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify(raw_body: bytes, headers: Mapping[str, str], key: str,
           tolerance_seconds: float | None = TOLERANCE_SECONDS, now: float | None = None) -> dict[str, Any]:
    """Return the parsed event if the signature is valid, else raise WebhookError."""
    if not key:
        raise WebhookError("no signing key configured")
    lowered = {k.lower(): v for k, v in headers.items()}
    signature = (lowered.get("nomba-signature") or lowered.get("nomba-sig-value") or "").strip()
    timestamp = (lowered.get("nomba-timestamp") or "").strip()
    if not signature or not timestamp:
        raise WebhookError("missing signature or timestamp header")
    if not _RFC3339.fullmatch(timestamp):
        raise WebhookError("bad timestamp")
    if tolerance_seconds is not None:
        current = time.time() if now is None else now
        if abs(current - _parse_time(timestamp)) > tolerance_seconds:
            raise WebhookError("timestamp outside tolerance")
    try:
        event = json.loads(raw_body)
    except ValueError:
        raise WebhookError("body is not JSON") from None
    if not isinstance(event, dict):
        raise WebhookError("body is not a JSON object")
    expected = compute_signature(event, timestamp, key)
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        raise WebhookError("signature does not match")
    return event


def parse_incoming_credit(event: Mapping[str, Any]) -> IncomingCredit | None:
    """An incoming bank transfer, or None for any other event type."""
    if event.get("event_type") != "payment_success":
        return None
    data = event.get("data") or {}
    txn = data.get("transaction") or {}
    customer = data.get("customer") or {}
    txn_time = txn.get("time")
    return IncomingCredit(
        transaction_id=str(txn.get("transactionId", "")),
        session_id=str(txn.get("sessionId", "")),
        amount_hint=Decimal(str(txn.get("transactionAmount", "0"))),
        sender_name=str(customer.get("senderName", "")),
        sender_bank=str(customer.get("bankName", "")),
        received_at=_parse_time(txn_time) if isinstance(txn_time, str) and txn_time else time.time(),
    )

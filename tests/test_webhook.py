import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot import webhook

FIXTURE = (Path(__file__).parent / "fixtures" / "nomba-webhook.json").read_bytes()
# Test vector from Nomba's webhook docs (also used by Nomba's official SDK).
KEY = "SampleSignatureKey123"
TIMESTAMP = "2025-09-29T10:51:44Z"
SIGNATURE = "q30yFZDyykoymBf9+mt1wz5UAuLk57raPGPHlGzIv00="
NOW = datetime.fromisoformat("2025-09-29T10:52:00+00:00").timestamp()


def headers(sig: str = SIGNATURE, ts: str = TIMESTAMP) -> dict[str, str]:
    return {"Nomba-Signature": sig, "Nomba-Timestamp": ts}


def test_documented_signature_verifies():
    event = webhook.verify(FIXTURE, headers(), KEY, now=NOW)
    assert event["event_type"] == "payment_success"


def test_computed_signature_matches_vector():
    assert webhook.compute_signature(json.loads(FIXTURE), TIMESTAMP, KEY) == SIGNATURE


@pytest.mark.parametrize("bad", [SIGNATURE.upper(), "Q" + SIGNATURE[1:], ""])
def test_bad_signature_is_rejected(bad):
    with pytest.raises(webhook.WebhookError):
        webhook.verify(FIXTURE, headers(sig=bad), KEY, now=NOW)


def test_wrong_key_is_rejected():
    with pytest.raises(webhook.WebhookError):
        webhook.verify(FIXTURE, headers(), "not-the-key", now=NOW)


def test_old_webhook_is_rejected_as_replay():
    with pytest.raises(webhook.WebhookError, match="tolerance"):
        webhook.verify(FIXTURE, headers(), KEY, now=NOW + 3600)


def test_tampered_transaction_id_is_rejected():
    event = json.loads(FIXTURE)
    event["data"]["transaction"]["transactionId"] = "something-else"
    with pytest.raises(webhook.WebhookError):
        webhook.verify(json.dumps(event).encode(), headers(), KEY, now=NOW)


def test_parse_incoming_credit():
    credit = webhook.parse_incoming_credit(json.loads(FIXTURE))
    assert credit.sender_name == "John Doe"
    assert credit.amount_hint == Decimal("10")
    assert credit.transaction_id.startswith("API-VACT_TRA-")
    assert credit.received_at == datetime.fromisoformat("2025-09-29T10:51:44+00:00").timestamp()


def test_other_events_are_not_credits():
    event = json.loads(FIXTURE)
    event["event_type"] = "payout_success"
    assert webhook.parse_incoming_credit(event) is None

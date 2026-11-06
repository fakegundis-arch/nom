"""Nomba Business API client.

Auth: POST /v1/auth/token/issue with client_id/client_secret and an
`accountId` header returns an access token. Every later call sends
`Authorization: Bearer <token>` plus the same `accountId` header.

Responses look like {"code": "00", "description": "...", "data": {...}}.
Paths and field names follow Nomba's official Python SDK (NombaOSS/nomba-python).
"""

import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests

SUCCESS_CODES = {"00", "200", "201"}

# Used when the token response has no expiresAt (the official SDK does the same).
FALLBACK_TOKEN_SECONDS = 10 * 60
# Refresh this long before the token expires.
TOKEN_MARGIN_SECONDS = 60


class NombaError(RuntimeError):
    pass


def _parse_expiry(value: Any, now: float) -> float:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return now + FALLBACK_TOKEN_SECONDS


class NombaClient:
    def __init__(self, client_id: str, client_secret: str, account_id: str, base_url: str,
                 monitor_only: bool = True, session: requests.Session | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_id = account_id
        self.base_url = base_url.rstrip("/")
        self.monitor_only = monitor_only
        self.http = session or requests.Session()
        self._token = ""
        self._token_expires_at = 0.0
        self._banks: list[dict[str, str]] | None = None

    # --- auth ---

    def _issue_token(self) -> None:
        resp = self.http.post(
            self.base_url + "/v1/auth/token/issue",
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"accountId": self.account_id},
            timeout=15,
        )
        data = self._unwrap(resp, "issue token")
        self._token = data["access_token"]
        self._token_expires_at = _parse_expiry(data.get("expiresAt"), time.time())

    def _headers(self) -> dict[str, str]:
        if not self._token or time.time() >= self._token_expires_at - TOKEN_MARGIN_SECONDS:
            self._issue_token()
        return {"Authorization": f"Bearer {self._token}", "accountId": self.account_id}

    @staticmethod
    def _unwrap(resp: requests.Response, what: str) -> Any:
        try:
            body = resp.json()
        except ValueError:
            raise NombaError(f"{what}: HTTP {resp.status_code}, non-JSON response") from None
        if resp.status_code >= 400 or str(body.get("code")) not in SUCCESS_CODES:
            raise NombaError(f"{what}: HTTP {resp.status_code} {body.get('code')} {body.get('description')}")
        return body.get("data")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self.http.request(method, self.base_url + path, headers=self._headers(), timeout=20, **kwargs)
        return self._unwrap(resp, f"{method} {path}")

    # --- read-only calls ---

    def balance(self) -> Decimal:
        data = self._request("GET", "/v1/accounts/balance")
        return Decimal(str(data["amount"]))

    def banks(self) -> list[dict[str, str]]:
        """[{"code": ..., "name": ...}, ...]. Cached, since codes rarely change."""
        if self._banks is None:
            data = self._request("GET", "/v1/transfers/banks")
            self._banks = data.get("results", []) if isinstance(data, dict) else data
        return self._banks

    def lookup_account_name(self, account_number: str, bank_code: str) -> str:
        data = self._request(
            "POST",
            "/v1/transfers/bank/lookup",
            json={"accountNumber": account_number, "bankCode": bank_code},
        )
        return data.get("accountName", "")

    def get_transaction(self, transaction_ref: str) -> dict[str, Any]:
        """Fetch a transaction from Nomba itself. Webhook signatures don't cover
        the amount, so the amount we act on always comes from here."""
        return self._request(
            "GET", "/v1/transactions/accounts/single", params={"transactionRef": transaction_ref}
        )

    # --- money-moving calls (blocked while monitor_only) ---

    def transfer_to_bank(self, amount: Decimal, account_number: str, account_name: str,
                         bank_code: str, reference: str, narration: str) -> dict[str, Any]:
        """Send naira. `reference` must be unique per order so a retry never pays twice."""
        if self.monitor_only:
            raise NombaError("Refusing to send money: MONITOR_ONLY is on")
        return self._request(
            "POST",
            "/v2/transfers/bank",
            json={
                "amount": float(amount),
                "accountNumber": account_number,
                "accountName": account_name,
                "bankCode": bank_code,
                "merchantTxRef": reference,
                "narration": narration,
            },
        )

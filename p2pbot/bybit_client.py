"""Bybit P2P Open API client.

Requests are signed with Bybit's v5 HMAC scheme:
    sign = HMAC_SHA256(secret, timestamp + api_key + recv_window + body)
P2P endpoints take JSON POST bodies.

Only advertisers (General Advertiser or above) can call these endpoints.
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import requests

RECV_WINDOW = "5000"

# Order status codes from the Bybit P2P docs.
STATUS_WAITING_BUYER_PAY = 10
STATUS_WAITING_SELLER_RELEASE = 20
STATUS_APPEALING = 30
STATUS_CANCELLED = 40
STATUS_FINISHED = 50

# Order side, from our point of view as the advertiser.
SIDE_BUY = 0
SIDE_SELL = 1


class BybitError(RuntimeError):
    pass


@dataclass
class PaymentTerm:
    real_name: str
    account_no: str
    bank_name: str


@dataclass
class P2POrder:
    order_id: str
    side: int
    status: int
    token: str
    currency: str
    price: Decimal
    quantity: Decimal
    amount: Decimal
    buyer_real_name: str
    seller_real_name: str
    counterparty_nickname: str
    created_ms: int
    payment_terms: list[PaymentTerm]

    @property
    def counterparty_real_name(self) -> str:
        return self.buyer_real_name if self.side == SIDE_SELL else self.seller_real_name


def sign(secret: str, timestamp: str, api_key: str, recv_window: str, payload: str) -> str:
    message = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def parse_order(raw: dict[str, Any]) -> P2POrder:
    terms = []
    for term in raw.get("paymentTermList") or []:
        terms.append(
            PaymentTerm(
                real_name=term.get("realName", ""),
                account_no=term.get("accountNo", ""),
                bank_name=term.get("bankName", ""),
            )
        )
    return P2POrder(
        order_id=str(raw["id"]),
        side=int(raw.get("side", -1)),
        status=int(raw.get("status", -1)),
        token=raw.get("tokenId", ""),
        currency=raw.get("currencyId", ""),
        price=Decimal(str(raw.get("price", "0"))),
        quantity=Decimal(str(raw.get("quantity", "0"))),
        amount=Decimal(str(raw.get("amount", "0"))),
        buyer_real_name=raw.get("buyerRealName", ""),
        seller_real_name=raw.get("sellerRealName", ""),
        counterparty_nickname=raw.get("targetNickName", ""),
        created_ms=int(raw.get("createDate", 0) or 0),
        payment_terms=terms,
    )


class BybitP2PClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str, monitor_only: bool = True,
                 session: requests.Session | None = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.monitor_only = monitor_only
        self.http = session or requests.Session()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        headers = {
            "Content-Type": "application/json",
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign(self.api_secret, timestamp, self.api_key, RECV_WINDOW, payload),
        }
        resp = self.http.post(self.base_url + path, data=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode") != 0:
            raise BybitError(f"{path}: {data.get('retCode')} {data.get('retMsg')}")
        return data.get("result") or {}

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        timestamp = str(int(time.time() * 1000))
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign(self.api_secret, timestamp, self.api_key, RECV_WINDOW, query),
        }
        resp = self.http.get(f"{self.base_url}{path}?{query}", headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode") != 0:
            raise BybitError(f"{path}: {data.get('retCode')} {data.get('retMsg')}")
        return data.get("result") or {}

    # --- read-only calls (safe in Phase 1) ---

    def pending_order_ids(self) -> list[str]:
        result = self._post("/v5/p2p/order/pending/simplifyList", {"page": 1, "size": 30})
        return [str(item["id"]) for item in result.get("items") or []]

    def order_detail(self, order_id: str) -> P2POrder:
        return parse_order(self._post("/v5/p2p/order/info", {"orderId": order_id}))

    def usdt_funding_balance(self) -> Decimal:
        result = self._get(
            "/v5/asset/transfer/query-account-coins-balance",
            {"accountType": "FUND", "coin": "USDT"},
        )
        for item in result.get("balance") or []:
            if item.get("coin") == "USDT":
                return Decimal(str(item.get("transferBalance") or item.get("walletBalance") or "0"))
        return Decimal("0")

    # --- ads (endpoints as in Bybit's official pybit SDK) ---

    def my_user_id(self) -> str:
        return str(self._post("/v5/p2p/user/personal/info", {}).get("userId", ""))

    def my_ads(self) -> list[dict[str, Any]]:
        return self._post("/v5/p2p/item/personal/list", {}).get("items") or []

    def ad_detail(self, item_id: str) -> dict[str, Any]:
        return self._post("/v5/p2p/item/info", {"itemId": item_id})

    def online_ads(self, token: str, currency: str, side: int) -> list[dict[str, Any]]:
        """Everyone's live ads on one side. side 0 = ads that buy USDT, 1 = ads that sell USDT."""
        body = {"tokenId": token, "currencyId": currency, "side": str(side), "page": "1", "size": "30"}
        return self._post("/v5/p2p/item/online", body).get("items") or []

    def update_ad(self, body: dict[str, Any]) -> None:
        """Edit (actionType MODIFY) or relist (actionType ACTIVE) an ad. Changes
        prices and limits only; it never moves money, so it isn't blocked by monitor_only."""
        self._post("/v5/p2p/item/update", body)

    def take_ad_offline(self, item_id: str) -> None:
        self._post("/v5/p2p/item/cancel", {"itemId": item_id})

    # --- money-moving calls (blocked while monitor_only) ---

    def _guard(self, action: str) -> None:
        if self.monitor_only:
            raise BybitError(f"Refusing to {action}: MONITOR_ONLY is on")

    def release_assets(self, order_id: str) -> None:
        self._guard("release crypto")
        self._post("/v5/p2p/order/finish", {"orderId": order_id})

    def mark_paid(self, order_id: str, payment_type: str, payment_id: str) -> None:
        self._guard("mark order paid")
        self._post(
            "/v5/p2p/order/pay",
            {"orderId": order_id, "paymentType": payment_type, "paymentId": payment_id},
        )

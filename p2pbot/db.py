"""SQLite storage: orders, bank credits and an audit log of everything the bot saw or did."""

import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .matcher import Credit

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id      TEXT PRIMARY KEY,
    side          INTEGER NOT NULL,
    status        INTEGER NOT NULL,
    amount        TEXT NOT NULL,
    quantity      TEXT NOT NULL,
    price         TEXT NOT NULL,
    counterparty  TEXT NOT NULL,
    created_ms    INTEGER NOT NULL,
    last_notice   TEXT,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS credits (
    txn_id        TEXT PRIMARY KEY,
    amount        TEXT NOT NULL,
    sender_name   TEXT NOT NULL,
    sender_bank   TEXT NOT NULL,
    received_at   REAL NOT NULL,
    used_by_order TEXT,
    raw           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    order_id  TEXT,
    message   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The webhook server writes from another thread.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    # --- audit log ---

    def log(self, kind: str, message: str, order_id: str | None = None) -> None:
        self._exec("INSERT INTO events (ts, kind, order_id, message) VALUES (?, ?, ?, ?)",
                   (time.time(), kind, order_id, message))

    # --- orders ---

    def upsert_order(self, order_id: str, side: int, status: int, amount: Decimal, quantity: Decimal,
                     price: Decimal, counterparty: str, created_ms: int) -> int | None:
        """Save the order. Returns the previous status, or None if it is new."""
        rows = self._exec("SELECT status FROM orders WHERE order_id = ?", (order_id,))
        previous = rows[0]["status"] if rows else None
        self._exec(
            """INSERT INTO orders (order_id, side, status, amount, quantity, price, counterparty, created_ms, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at""",
            (order_id, side, status, str(amount), str(quantity), str(price), counterparty, created_ms, time.time()),
        )
        return previous

    def last_notice(self, order_id: str) -> str | None:
        rows = self._exec("SELECT last_notice FROM orders WHERE order_id = ?", (order_id,))
        return rows[0]["last_notice"] if rows else None

    def set_last_notice(self, order_id: str, notice: str) -> None:
        self._exec("UPDATE orders SET last_notice = ? WHERE order_id = ?", (notice, order_id))

    def order_counts(self) -> dict[int, int]:
        rows = self._exec("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # --- bank credits ---

    def add_credit(self, txn_id: str, amount: Decimal, sender_name: str, sender_bank: str,
                   received_at: float, raw: dict[str, Any]) -> bool:
        """Returns False if this credit was already stored (webhooks can be resent)."""
        rows = self._exec(
            """INSERT OR IGNORE INTO credits (txn_id, amount, sender_name, sender_bank, received_at, raw)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING txn_id""",
            (txn_id, str(amount), sender_name, sender_bank, received_at, json.dumps(raw)),
        )
        return bool(rows)

    def credits_since(self, since: float) -> list[Credit]:
        rows = self._exec("SELECT * FROM credits WHERE received_at >= ?", (since,))
        return [Credit(r["txn_id"], Decimal(r["amount"]), r["sender_name"], r["received_at"], r["used_by_order"])
                for r in rows]

    def mark_credit_used(self, txn_id: str, order_id: str) -> bool:
        """Claim a credit for an order. False if another order already claimed it."""
        rows = self._exec(
            "UPDATE credits SET used_by_order = ? WHERE txn_id = ? AND used_by_order IS NULL RETURNING txn_id",
            (order_id, txn_id),
        )
        return bool(rows)

    # --- small key/value store for alert state ---

    def get_flag(self, key: str) -> str | None:
        rows = self._exec("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_flag(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, value))

"""
store.py — tiny SQLite persistence layer for orders + access codes.

No API keys are ever stored here. This file only ever touches Razorpay
order/payment identifiers and randomly-generated access codes.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parent / "data" / "app.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    amount      INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'created',   -- created | paid
    payment_id  TEXT,
    created_at  REAL NOT NULL,
    paid_at     REAL
);

CREATE TABLE IF NOT EXISTS access_codes (
    code        TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
"""


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_order(order_id: str, amount: int, currency: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO orders (order_id, amount, currency, status, created_at) "
            "VALUES (?, ?, ?, 'created', ?)",
            (order_id, amount, currency, time.time()),
        )


def mark_order_paid(order_id: str, payment_id: str) -> str | None:
    """
    Marks an order paid (idempotent) and returns the access code for it —
    reusing an existing code if this order was already marked paid before
    (e.g. both the client callback and the webhook fired).
    """
    with _conn() as c:
        row = c.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            return None

        c.execute(
            "UPDATE orders SET status = 'paid', payment_id = ?, paid_at = ? "
            "WHERE order_id = ? AND status != 'paid'",
            (payment_id, time.time(), order_id),
        )

        existing = c.execute(
            "SELECT code FROM access_codes WHERE order_id = ?", (order_id,)
        ).fetchone()
        if existing:
            return existing[0]

        code = _generate_code()
        c.execute(
            "INSERT INTO access_codes (code, order_id, created_at) VALUES (?, ?, ?)",
            (code, order_id, time.time()),
        )
        return code


def order_is_paid(order_id: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        return bool(row and row[0] == "paid")


def code_is_valid(code: str) -> bool:
    """Constant-time-ish lookup — code space is 160 bits, so guessing is
    not a realistic attack vector; this just checks it was actually issued."""
    with _conn() as c:
        row = c.execute("SELECT 1 FROM access_codes WHERE code = ?", (code,)).fetchone()
        return row is not None


def _generate_code() -> str:
    # 26 chars of base32-ish urlsafe randomness -> effectively unguessable.
    return "AR-" + secrets.token_urlsafe(20).replace("_", "").replace("-", "")[:26].upper()

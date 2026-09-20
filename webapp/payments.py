"""
payments.py — Razorpay integration.

Razorpay (not Stripe) because Stripe does not onboard India-based accounts.
Razorpay supports UPI, cards, netbanking and wallets and is the standard
India-first alternative.

Two independent layers of verification are used, deliberately:

1. Client-side callback -> /api/verify-payment: cryptographically checked
   with HMAC-SHA256 using your key secret (razorpay's own signature scheme).
   This alone is sufficient to prove the payment happened — a client cannot
   forge a signature without knowing key_secret.
2. Server-to-server webhook -> /webhook/razorpay: a defense-in-depth backstop
   for the case where the buyer's browser closes/crashes right after paying,
   before the client-side callback fires. Verified with a *separate* webhook
   secret you configure in the Razorpay dashboard.
"""
from __future__ import annotations

import os
import razorpay

KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

if not KEY_ID or not KEY_SECRET:
    raise RuntimeError(
        "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. "
        "Get live or test keys from https://dashboard.razorpay.com/app/keys"
    )

_client = razorpay.Client(auth=(KEY_ID, KEY_SECRET))


def create_order(amount_inr: int) -> dict:
    """amount_inr is a whole-rupee price, e.g. 499 for ₹499."""
    order = _client.order.create({
        "amount": amount_inr * 100,   # Razorpay wants paise
        "currency": "INR",
        "payment_capture": 1,
    })
    return order


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    try:
        _client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
        return True
    except razorpay.errors.SignatureVerificationError:
        return False


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        return False
    try:
        _client.utility.verify_webhook_signature(
            raw_body.decode("utf-8"), signature, WEBHOOK_SECRET
        )
        return True
    except razorpay.errors.SignatureVerificationError:
        return False

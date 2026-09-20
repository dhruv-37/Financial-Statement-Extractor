"""
app.py — AR Extractor: paywalled front end for the Step1+Step2 pipeline.

Flow
----
1. GET  /                    Landing page, "Get access" button.
2. POST /api/create-order    Creates a Razorpay order, returns it to the browser.
3. (Razorpay Checkout.js runs client-side)
4. POST /api/verify-payment  Verifies signature server-side, issues an access
                              code, sets a signed session cookie for THIS browser.
5. POST /webhook/razorpay    Server-to-server backstop (see payments.py).
6. GET/POST /redeem          For a second device: enter the access code shown
                              after payment to unlock this browser too.
7. GET  /tool                The actual extractor UI (Gemini key + PDF upload).
   POST /tool/run            Runs the pipeline, returns the .xlsx.

Session cookie only ever holds a boolean + the access code string — never
a Gemini key, never payment secrets.
"""
from __future__ import annotations

import os
import logging
from flask import (
    Flask, render_template, request, session, redirect,
    url_for, jsonify, send_file, abort,
)
import io

import store
import payments
from pipeline_runner import run as run_pipeline, PipelineError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("arx")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is not set.")

app.config.update(
    MAX_CONTENT_LENGTH=40 * 1024 * 1024,   # 40 MB upload cap, enforced by Flask itself
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
)

PRICE_INR = int(os.environ.get("PRICE_INR", "499"))
APP_NAME = os.environ.get("APP_NAME", "Annual Report Extractor")


# ── access control ────────────────────────────────────────────────────────

def _has_access() -> bool:
    return bool(session.get("paid"))


def _require_access():
    if not _has_access():
        abort(redirect(url_for("landing")))


# ── pages ──────────────────────────────────────────────────────────────────

@app.route("/")
def landing():
    return render_template(
        "landing.html",
        app_name=APP_NAME,
        price=PRICE_INR,
        razorpay_key_id=payments.KEY_ID,
        has_access=_has_access(),
    )


@app.route("/redeem", methods=["GET", "POST"])
def redeem():
    error = None
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        if store.code_is_valid(code):
            session["paid"] = True
            session["access_code"] = code
            return redirect(url_for("tool"))
        error = "That code isn't recognised. Check it and try again."
    return render_template("redeem.html", app_name=APP_NAME, error=error)


@app.route("/tool")
def tool():
    if not _has_access():
        return redirect(url_for("landing"))
    return render_template("tool.html", app_name=APP_NAME, access_code=session.get("access_code", ""))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))

@app.route("/dev-unlock")
def dev_unlock():
    # Local development only — grants access without payment.
    # Refuses to run at all once FLASK_ENV=production, so it can never
    # accidentally ship live.
    if os.environ.get("FLASK_ENV") == "production":
        abort(404)
    session["paid"] = True
    session["access_code"] = "DEV-BYPASS"
    return redirect(url_for("tool"))

# ── payment API ──────────────────────────────────────────────────────────

@app.post("/api/create-order")
def api_create_order():
    order = payments.create_order(PRICE_INR)
    store.create_order(order["id"], order["amount"], order["currency"])
    return jsonify({
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": payments.KEY_ID,
        "app_name": APP_NAME,
    })


@app.post("/api/verify-payment")
def api_verify_payment():
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("razorpay_order_id", "")
    payment_id = data.get("razorpay_payment_id", "")
    signature = data.get("razorpay_signature", "")

    if not (order_id and payment_id and signature):
        return jsonify({"ok": False, "error": "Missing payment fields."}), 400

    if not payments.verify_payment_signature(order_id, payment_id, signature):
        log.warning("Payment signature verification FAILED for order %s", order_id)
        return jsonify({"ok": False, "error": "Payment could not be verified."}), 400

    code = store.mark_order_paid(order_id, payment_id)
    if not code:
        return jsonify({"ok": False, "error": "Unknown order."}), 400

    session["paid"] = True
    session["access_code"] = code
    return jsonify({"ok": True, "access_code": code, "redirect": url_for("tool")})


@app.post("/webhook/razorpay")
def webhook_razorpay():
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not payments.verify_webhook_signature(raw_body, signature):
        log.warning("Webhook signature verification failed")
        return jsonify({"ok": False}), 400

    payload = request.get_json(silent=True) or {}
    event = payload.get("event", "")
    if event == "payment.captured":
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        order_id = entity.get("order_id", "")
        payment_id = entity.get("id", "")
        if order_id and payment_id:
            store.mark_order_paid(order_id, payment_id)
            log.info("Webhook confirmed payment for order %s", order_id)

    return jsonify({"ok": True})


# ── the actual tool ─────────────────────────────────────────────────────

@app.post("/tool/run")
def tool_run():
    if not _has_access():
        return jsonify({"ok": False, "error": "Access required."}), 403

    uploaded = request.files.get("pdf")
    ticker = (request.form.get("ticker") or "REPORT").strip()
    api_key = (request.form.get("gemini_api_key") or "").strip()

    if uploaded is None or uploaded.filename == "":
        return jsonify({"ok": False, "error": "Please choose a PDF file."}), 400
    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "Only PDF files are accepted."}), 400

    pdf_bytes = uploaded.read()

    try:
        xlsx_bytes, filename = run_pipeline(pdf_bytes, ticker, api_key)
    except PipelineError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        # Explicitly drop references — the request-scoped variables holding
        # the key and the PDF bytes are not reused past this point.
        api_key = None
        pdf_bytes = None

    return send_file(
        io.BytesIO(xlsx_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    # Local dev only. In production run behind gunicorn (see README_DEPLOY.md)
    # with debug=False so tracebacks are never shown to visitors.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

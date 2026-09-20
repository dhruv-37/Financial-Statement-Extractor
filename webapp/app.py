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
import io
import time
import uuid
import logging
import threading
from flask import (
    Flask, render_template, request, session, redirect,
    url_for, jsonify, send_file, abort,
)

import store
import payments
from pipeline_runner import run as run_pipeline, PipelineError
from pipeline.phase1_filter_batch import extract_auditor_signatures

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

# In-memory job registry for progress polling. Fine for a single-process
# server (dev, or gunicorn -w 1). If you scale to multiple gunicorn workers
# later, this needs to move to something shared (e.g. Redis) — a poll for
# job X could otherwise hit a worker that never ran job X.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOB_TTL_SECONDS = 30 * 60


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(fields)


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _prune_old_jobs() -> None:
    cutoff = time.time() - _JOB_TTL_SECONDS
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items() if j.get("created_at", 0) < cutoff]
        for jid in stale:
            _jobs.pop(jid, None)


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


@app.route("/preview")
def preview():
    return render_template("preview.html", app_name=APP_NAME, has_access=_has_access())


@app.post("/preview/run")
def preview_run():
    """
    Free, no key, no payment. Runs ONLY the pure regex/PyMuPDF signature
    detection (pipeline/phase1_filter_batch.py) — there is no Gemini call
    anywhere in this route, so it costs nothing per visitor and needs no key
    from anyone. Shows what the tool found; the actual Excel/PDF output
    still requires payment + the visitor's own Gemini key.
    """
    import tempfile, os as _os

    uploaded = request.files.get("pdf")
    if uploaded is None or uploaded.filename == "":
        return jsonify({"ok": False, "error": "Please choose a PDF file."}), 400
    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "Only PDF files are accepted."}), 400

    pdf_bytes = uploaded.read()
    if pdf_bytes[:4] != b"%PDF":
        return jsonify({"ok": False, "error": "That file doesn't look like a PDF."}), 400
    if len(pdf_bytes) > 40 * 1024 * 1024:
        return jsonify({"ok": False, "error": "File too large (max 40 MB)."}), 400

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
        with _os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)

        results = extract_auditor_signatures(tmp_path, no_pdf=True)
        sections = sorted({r["section"] for r in results if r.get("section")})
        sample = [
            {"page_number": r["page_number"], "section": r.get("section") or "Unknown"}
            for r in sorted(results, key=lambda r: r["page_number"])[:8]
        ]
        return jsonify({
            "ok": True,
            "candidate_pages": len(results),
            "sections_found": sections,
            "sample": sample,
        })
    except Exception as exc:  # noqa: BLE001
        log.exception("Preview failed")
        return jsonify({"ok": False, "error": "Could not process that PDF."}), 400
    finally:
        if tmp_path and _os.path.exists(tmp_path):
            _os.remove(tmp_path)


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


# ── the actual tool (async job + progress polling) ──────────────────────

def _run_job(job_id: str, pdf_bytes: bytes, ticker: str, api_key: str) -> None:
    def progress_cb(pct: int, message: str) -> None:
        _set_job(job_id, percent=pct, message=message)

    def summary_cb(bullets) -> None:
        _set_job(job_id, summary=bullets, summary_ready=True)

    try:
        zip_bytes, filename = run_pipeline(
            pdf_bytes, ticker, api_key,
            progress_cb=progress_cb, summary_cb=summary_cb,
        )
        _set_job(job_id, done=True, error=None, result=zip_bytes, filename=filename, percent=100)
    except PipelineError as exc:
        _set_job(job_id, done=True, error=str(exc), result=None)
    except Exception:  # noqa: BLE001
        log.exception("Unhandled error in job %s", job_id)
        _set_job(job_id, done=True, error="Unexpected server error — try again.", result=None)


@app.post("/tool/run")
def tool_run():
    if not _has_access():
        return jsonify({"ok": False, "error": "Access required."}), 403

    _prune_old_jobs()

    uploaded = request.files.get("pdf")
    ticker = (request.form.get("ticker") or "REPORT").strip()
    api_key = (request.form.get("gemini_api_key") or "").strip()

    if uploaded is None or uploaded.filename == "":
        return jsonify({"ok": False, "error": "Please choose a PDF file."}), 400
    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "Only PDF files are accepted."}), 400

    pdf_bytes = uploaded.read()
    job_id = uuid.uuid4().hex

    _set_job(job_id, percent=0, message="Queued…", done=False, error=None,
              result=None, summary=None, summary_ready=False, created_at=time.time())

    thread = threading.Thread(
        target=_run_job, args=(job_id, pdf_bytes, ticker, api_key), daemon=True,
    )
    thread.start()
    # pdf_bytes/api_key now only live inside the thread's stack frame and are
    # dropped by pipeline_runner.run() itself when the job finishes.
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/tool/status/<job_id>")
def tool_status(job_id):
    if not _has_access():
        return jsonify({"ok": False, "error": "Access required."}), 403
    job = _get_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "Unknown or expired job."}), 404
    return jsonify({
        "ok": True,
        "percent": job.get("percent", 0),
        "message": job.get("message", ""),
        "done": job.get("done", False),
        "error": job.get("error"),
        "summary_ready": job.get("summary_ready", False),
        "summary": job.get("summary"),
    })


@app.get("/tool/result/<job_id>")
def tool_result(job_id):
    if not _has_access():
        return jsonify({"ok": False, "error": "Access required."}), 403
    job = _get_job(job_id)
    if job is None or not job.get("done") or job.get("error") or job.get("result") is None:
        return jsonify({"ok": False, "error": "Result not ready."}), 400

    zip_bytes = job["result"]
    filename = job["filename"]
    # Deliberately NOT deleted here — a refresh, back button, or a duplicate
    # fetch (as seen from real logs) must not turn a real download into an
    # error. It's cleaned up later by _prune_old_jobs()'s TTL instead.

    return send_file(
        io.BytesIO(zip_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype="application/zip",
    )


if __name__ == "__main__":
    # Local dev only. In production run behind gunicorn (see README_DEPLOY.md)
    # with debug=False so tracebacks are never shown to visitors.
    # threaded=True so the progress-polling requests aren't blocked behind
    # the background extraction thread on the dev server.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)

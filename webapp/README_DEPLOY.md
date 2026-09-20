# Deploying AR Extractor (paywalled)

## Why not Stripe
Stripe doesn't onboard India-registered businesses/individuals. **Razorpay**
is used instead — supports UPI, cards, netbanking, wallets, and settles to
Indian bank accounts.

## 1. Get Razorpay keys
1. Sign up at https://dashboard.razorpay.com (complete KYC to go live; test
   mode works immediately for development).
2. Settings → API Keys → generate Key Id / Key Secret.
3. Put them in `.env` (copy `.env.example` first).

## 2. Layout
Put this `webapp/` folder **next to** your existing `pipeline/` folder, i.e.:

```
Financial-Statement-Extractor/
  pipeline/          <- unchanged, already patched (Step2 now takes api_key)
  webapp/            <- this folder
    app.py
    store.py
    payments.py
    pipeline_runner.py
    templates/
    requirements-web.txt
    .env.example
```

`pipeline_runner.py` imports `from pipeline.Step1 import ...` — run the app
from `Financial-Statement-Extractor/` (one level above `webapp/`), or add
that directory to `PYTHONPATH`.

## 3. Install
```bash
cd Financial-Statement-Extractor
pip install -r requirements.txt          # pipeline deps (pymupdf, pypdf, etc.)
pip install -r webapp/requirements-web.txt
cp webapp/.env.example webapp/.env
# fill in webapp/.env
```

## 4. Run locally
```bash
export $(cat webapp/.env | xargs)   # or use python-dotenv / your shell's own method
python webapp/app.py
# -> http://127.0.0.1:5000
```
Use Razorpay **test mode** keys and their test card (4111 1111 1111 1111,
any future expiry/CVV) to try the full buy → redeem → run flow before going live.

## 5. Host it somewhere that allows long-running requests
The extraction can take from several seconds to a couple of minutes
depending on the PDF. **Do not** deploy to a platform with short serverless
request timeouts (e.g. Vercel/Netlify functions). Use:
- **Render** or **Railway** — simplest, both auto-detect gunicorn, free TLS.
- **Fly.io** or a small VPS (DigitalOcean/Hetzner) — more control.

Start command for all of them:
```bash
cd Financial-Statement-Extractor
gunicorn -w 2 --timeout 180 --chdir . webapp.app:app
```
(`--timeout 180` — raise further if you see very large PDFs timing out.)

Set the same env vars from `.env` in the host's dashboard — **do not commit
`.env`** (already in `webapp/.gitignore`).

## 6. Add the Razorpay webhook (defense-in-depth, not strictly required)
Dashboard → Settings → Webhooks → Add New Webhook:
- URL: `https://yourdomain.com/webhook/razorpay`
- Active events: `payment.captured`
- Copy the generated **Webhook Secret** into `RAZORPAY_WEBHOOK_SECRET`.

This catches the rare case where a buyer's browser closes right after
paying, before the on-page callback fires — the payment still gets marked
paid server-side. (They just wouldn't have seen their access code — add a
"lost your code" support contact for that edge case, or extend
`webhook_razorpay()` to email it if you wire up an email provider.)

## 7. Go live
Switch the Razorpay dashboard from Test to Live mode, swap in live keys,
set `FLASK_ENV=production` (this turns on `Secure` cookies — only do this
once you're actually serving over HTTPS).

## Security checklist (what "no leakage" means here)
- [x] Gemini API key: request-scoped variable only. Never logged, never in
      a cookie/session/DB, deleted (`del`) right after use. A fresh
      `google.genai.Client` is built per request in `Step2.py` — no shared
      global client/key across users (this was a real bug in the original
      single-user script, fixed as part of this change).
- [x] Payment: verified server-side via HMAC signature using your Razorpay
      key secret (`payments.verify_payment_signature`) — a browser cannot
      forge "I paid" without knowing that secret. Webhook is a second,
      independent check.
- [x] Access codes: 26 chars of `secrets.token_urlsafe` randomness (not
      sequential/guessable), stored server-side, checked by DB lookup.
- [x] Uploads: extension + magic-byte (`%PDF`) checked, size capped at 40MB
      via both `MAX_CONTENT_LENGTH` and an app-level check.
- [x] Temp files: every run uses its own `tempfile.mkdtemp()`, deleted in a
      `finally` block regardless of success/failure. The finished .xlsx is
      read into memory before the directory is deleted.
- [x] Cookies: `HttpOnly`, `SameSite=Lax`, `Secure` in production.
- [x] Debug mode off (`debug=False`) — no stack traces or local variables
      (which could include a key mid-request) ever reach the browser.
- [ ] Consider adding a request-rate limit (e.g. `flask-limiter`) on
      `/tool/run` if you see abuse — not included here to keep the base
      app dependency-light.
- [ ] Periodically clear `output/step2_cache/` on the server — it's shared
      across users but keyed by content hash (never by api_key), so it's
      not a privacy issue, just disk growth over time.

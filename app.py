"""
Known activation backend.

Minimal Flask service with a single job: claim a sticker code during USB
provisioning. Everything else about Known is local/on-device — this is the only
cloud surface.

    POST /activate
        body: { "sticker_code": "KNOWN-XXXX-XXXX", "user_id": "..."? }
        200:  { "status": "ok", "device_id": "..." }
        4xx:  { "status": "error", "reason": "already_claimed" | ... }

    GET  /stats
        200:  { "total": N, "claimed": N, "claim_rate": 0.0..1.0,
                "by_day": [{ "date": "YYYY-MM-DD", "claimed": N }, ...] }

Run (dev):
    pip install -r requirements.txt
    python app.py            # serves on http://localhost:8000

CORS is enabled so the dashboard (a different origin in dev) can call it.
"""

import logging
import re
import time

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import db

STICKER_RE = re.compile(r"^KNOWN-[A-Z0-9]{4}-[A-Z0-9]{4}$")

# Plain stderr log — we run behind `flask run` / `python app.py` and a real
# logger config can be swapped in later. INFO for activations, WARNING for
# rate-limit hits and bad input.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("known-activate")

app = Flask(__name__)
CORS(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"]
)

db.init_db()


@app.errorhandler(429)
def rate_limited(e):
    # flask-limiter sets e.description; the IP is the limit key.
    ip = request.remote_addr or "unknown"
    log.warning("rate_limit_hit ip=%s path=%s limit=%s",
                ip, request.path, e.description)
    return jsonify(status="error", reason="rate_limited"), 429


@app.post("/activate")
@limiter.limit("5 per minute")
def activate():
    data = request.get_json(silent=True) or {}
    code = (data.get("sticker_code") or "").strip().upper()
    user_id = data.get("user_id") or "anonymous"
    site_id = data.get("site_id")
    organization_id = data.get("organization_id")
    signature = data.get("signature")
    ip = request.remote_addr or "unknown"

    if not STICKER_RE.match(code):
        log.info("activate ip=%s code=%s reason=bad_format", ip, code or "<empty>")
        return jsonify(status="error", reason="bad_format"), 400

    # Per-IP claim limit: a legitimate user claims one device.
    # An IP claiming 3+ in 24h is likely scraping codes.
    MAX_CLAIMS_PER_IP = 3
    existing_claims = db.claims_by_ip_today(ip)
    if existing_claims >= MAX_CLAIMS_PER_IP:
        log.warning("activate ip=%s code=%s reason=ip_claim_limit (%d claims today)",
                    ip, code, existing_claims)
        return jsonify(status="error", reason="ip_limit"), 429

    ok, result = db.claim_device(code, user_id)
    if not ok:
        reason = "not_found" if result == "not_found" else "already_claimed"
        log.info("activate ip=%s code=%s reason=%s", ip, code, reason)
        http = 404 if result == "not_found" else 409
        return jsonify(status="error", reason=reason), http

    # On the success branch `result` is the device dict from get_device.
    assert isinstance(result, dict)

    # Log signature if provided (not enforced yet — primitive is here so
    # enforcement can be flipped on at 1000+ devices without a rebuild).
    if signature:
        log.info("activate ip=%s code=%s signature=provided (not verified)", ip, code)
    else:
        log.info("activate ip=%s code=%s signature=none", ip, code)

    # Stamp site/org metadata onto the claimed device if provided.
    # These are optional — consumer activations won't include them.
    # MSP/B2B activations will, so an MSP can group devices per client site.
    if site_id or organization_id:
        db.set_device_metadata(code, site_id=site_id,
                               organization_id=organization_id)
        result = db.get_device(code)

    log.info("activate ip=%s code=%s device_id=%s reason=ok",
             ip, code, result["device_id"])
    return jsonify(status="ok", device_id=result["device_id"]), 200


@app.get("/stats")
def stats():
    """Lightweight analytics. Counts rows, groups claims by day."""
    total, claimed = db.count_devices()
    by_day = db.claimed_per_day()
    rate = (claimed / total) if total else 0.0
    return jsonify(
        total=total,
        claimed=claimed,
        claim_rate=round(rate, 4),
        by_day=by_day,
    ), 200


@app.get("/health")
def health():
    return jsonify(status="ok"), 200


@app.get("/challenge")
def challenge():
    """Return a nonce for signed activation. The PWA signs the sticker code
    with the device_secret (from manufacturing config.json) and sends the
    signature with /activate. Not enforced yet — the primitive is here so
    enforcement can be flipped on without a rebuild at 1000+ devices."""
    import secrets as _secrets
    nonce = _secrets.token_hex(16)
    # Store nonce in a simple in-memory dict keyed by nonce.
    # TTL is short — the PWA uses it immediately. For production, use Redis.
    if not hasattr(app, '_challenge_store'):
        app._challenge_store = {}
    app._challenge_store[nonce] = time.time()
    # Clean up old challenges (older than 5 minutes)
    now = time.time()
    app._challenge_store = {
        k: v for k, v in app._challenge_store.items() if now - v < 300
    }
    return jsonify(status="ok", nonce=nonce), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)


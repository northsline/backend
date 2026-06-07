"""
Known activation backend.

Minimal Flask service with a single job: claim a sticker code during USB
provisioning. Everything else about Known is local/on-device — this is the only
cloud surface.

    POST /activate
        body: { "sticker_code": "KNOWN-XXXX-XXXX", "user_id": "..."? }
        200:  { "status": "ok", "device_id": "..." }
        4xx:  { "status": "error", "reason": "already_claimed" | ... }

Run (dev):
    pip install -r requirements.txt
    python app.py            # serves on http://localhost:8000

CORS is enabled so the dashboard (a different origin in dev) can call it.
"""

import re

from flask import Flask, request, jsonify
from flask_cors import CORS

import db

STICKER_RE = re.compile(r"^KNOWN-[A-Z0-9]{4}-[A-Z0-9]{4}$")

app = Flask(__name__)
CORS(app)

db.init_db()


@app.post("/activate")
def activate():
    data = request.get_json(silent=True) or {}
    code = (data.get("sticker_code") or "").strip().upper()
    user_id = data.get("user_id") or "anonymous"

    if not STICKER_RE.match(code):
        return jsonify(status="error", reason="bad_format"), 400

    ok, result = db.claim_device(code, user_id)
    if not ok:
        http = 404 if result == "not_found" else 409
        return jsonify(status="error", reason=result), http

    return jsonify(status="ok", device_id=result["device_id"]), 200


@app.get("/health")
def health():
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

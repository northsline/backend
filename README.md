# Known Activation Backend

Minimal Flask service for Known device activation. The only cloud
surface in the product: it claims a sticker code (single-use) during USB
provisioning. Everything else runs on-device or in the local dashboard.

## Run (dev)

```bash
pip install -r requirements.txt
python generate_codes.py     # seed 10 test codes into codes.db
python app.py                # http://localhost:8000
```

The dashboard reads its target from `VITE_API_BASE_URL` (default
`http://localhost:8000`). Set that env var to your deployed host in production.

## Endpoint

```
POST /activate
  body: { "sticker_code": "KNOWN-XXXX-XXXX", "user_id"?: "..." }
  200:  { "status": "ok", "device_id": "..." }
  400:  { "status": "error", "reason": "bad_format" }
  404:  { "status": "error", "reason": "not_found" }
  409:  { "status": "error", "reason": "already_claimed" }
```

## Files

- `app.py` — Flask app, `POST /activate` + `GET /health`, CORS enabled.
- `db.py` — SQLite registry schema + claim logic.
- `generate_codes.py` — seed test devices (code + secret + id).
- `codes.db` — generated SQLite file (gitignored).

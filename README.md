# Known Backend

Manufacturing tools for Known devices. No server, no cloud — the backend
directory is a toolbox for generating cryptographic keys and flashing devices.

## What's here

- `keygen.py` — Generate the Northsline root key pair and per-device ECDSA
  P-256 keys + certificates. Run `--init-root` once, then `--device` for
  each unit.
- `flash_known.py` — One-command manufacturing: generate device keys, flash
  MicroPython UF2, copy firmware, burn keys to the device, verify, and
  generate a printable serial sticker (PNG + optional A4 PDF).
- `sticker.py` — Generate a printable device serial sticker (QR code +
  serial number). Called automatically by `flash_known.py`, or standalone
  for reprinting lost stickers.
- `test_crypto_pipeline.py` — End-to-end crypto verification. Confirms the
  pure-Python ECDSA signer (firmware) produces signatures compatible with
  the cryptography library (same backend as Web Crypto).
- `keys/` — Root key pair. `root_private.pem` (600 permissions, never leaves
  this machine), `root_public.pem`, `root_public.jwk` (embedded in the PWA).
- `stickers/` — Generated sticker PNGs, filed by serial number.

## What was removed

The cloud activation backend (`app.py`, `db.py`, `codes.db`, `generate_codes.py`,
`test_backend.py`, `test_api_logic.py`) was deleted on 2026-06-20. Device
activation is now fully offline via cryptographic challenge-response over USB.
See `docs/internal/project/local-crypto-provisioning.md` for the design.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install cryptography mpremote qrcode[pil]
```

## Manufacturing flow

```bash
# 1. Generate root key pair (once, ever)
python keygen.py --init-root
# → paste the JWK output into onboard/src/lib/root-key.ts

# 2. Per device
python flash_known.py
# → generates keys, flashes firmware, burns keys, verifies,
#   saves stickers/SERIAL.png (print it, stick it on the device)

# 3. Reprint a lost sticker
python sticker.py SERIAL_HEX
python sticker.py SERIAL_HEX --pdf sheet.pdf   # A4 PDF
```

## License

See the Northsline project for licensing.
# Known Backend

Manufacturing tools + CLI for Known devices. No server, no cloud — this
directory is a toolbox for generating cryptographic keys, flashing devices,
and monitoring your network from the terminal.

## What's here

### Manufacturing tools

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

### CLI tool (`known-cli/`)

Terminal stats viewer for your Known device. Talks to the firmware's HTTP
API on port 8080. Zero firmware changes, zero external dependencies.

**Install:**

```bash
pip install ./known-cli
```

This puts `known` on your PATH.

**Commands:**

```
known                       pretty summary (default — stats grid, devices, activity)
known report                report with formatting options
known report --table        compact table format
known report --json         raw JSON (pipe to jq, grep, etc.)
known report --compact      one-line status
known report --scope devices  devices only (also: stats, activity, all)
known watch                 live refresh every 5s
known follow <ip>           live stream of DNS queries from one device
known diff                  what changed since last run (snapshot-based)
known monitor               silent check — exit 0 if OK, exit 1 if alert
known allow add <pattern>   add allowlist entry
known allow rm <id>         remove allowlist entry
known allow ls              list allowlist entries
known debug                 raw internal state from /debug
known --host 192.168.1.42   manual device IP (skip mDNS)
```

**What the CLI can do that the dashboard can't:**

- Follow a single device's queries in real time (`known follow`)
- Detect what changed between runs (`known diff` — saves snapshot to ~/.known-snapshot)
- Alert in cron jobs and shell prompts (`known monitor` — exit code based)
- Pipe to other tools (`known report --json | jq ...`)
- Manage the allowlist faster than clicking through UI

**Discovery:** Same logic as the dashboard — tries `known.local` first,
falls back to saved IP in `~/.known-host`, then manual `--host`.

## What was removed

The cloud activation backend (`app.py`, `db.py`, `codes.db`, `generate_codes.py`,
`test_backend.py`, `test_api_logic.py`) was deleted on 2026-06-20. Device
activation is now fully offline via cryptographic challenge-response over USB.
See `docs/internal/project/local-crypto-provisioning.md` for the design.

## Setup (manufacturing)

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
"""
Generate test device records for the Known registry.

Each device gets a sticker code (KNOWN-XXXX-XXXX), a device secret, and a
device id, mirroring what manufacturing injects into the per-device config.json.
By default this seeds 10 unclaimed test devices into codes.db and prints them so
you can paste a code into the dashboard during local provisioning.

Usage:
    python generate_codes.py            # 10 codes
    python generate_codes.py 25         # 25 codes
"""

import secrets
import sys
import uuid

import db

# Sticker alphabet. O/0/I/1 removed to prevent ambiguity on printed stickers.
# A user who can't tell O from 0 will fail activation and return the device.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_code():
    chars = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"KNOWN-{chars[:4]}-{chars[4:]}"


def generate_devices(count, site_id=None, organization_id=None):
    db.init_db()
    created = []
    for _ in range(count):
        code = generate_code()
        if db.get_device(code):  # negligible odds, but stay correct
            continue
        device_secret = secrets.token_hex(32)  # 256-bit
        device_id = str(uuid.uuid4())
        db.insert_device(code, device_secret, device_id,
                         site_id=site_id, organization_id=organization_id)
        created.append((code, device_id))
    return created


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    devices = generate_devices(n)
    print(f"Seeded {len(devices)} device(s) into {db.DB_PATH}:\n")
    for code, device_id in devices:
        print(f"  {code}   device_id={device_id}")

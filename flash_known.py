#!/usr/bin/env python3
"""
flash_known.py. One-command manufacturing script for Heron.

Does the full per-device setup in one shot:
  1. Generate device cryptographic keys (ECDSA P-256 key pair + root-signed cert)
  2. Wait for Pico in BOOTSEL mode, flash MicroPython UF2
  3. Copy firmware (main.py + lib/) to the device
  4. Inject /config.json with device token
  5. Burn device keys (private key, public key, serial, certificate) to key storage
  6. Serial verify with the identify command (checks keys are present)
  7. Print the device serial number for labeling

Usage:
    python flash_known.py                    # generate keys, flash, verify
    python flash_known.py --skip-uf2         # skip UF2 flash (already flashed)

Requires:
    - mpremote (pip install mpremote)
    - Pico 2 W connected via USB
    - MicroPython UF2 file at ../firmware/micropython-pico2.uf2 (or pass --uf2)
    - Root key pair generated (run keygen.py --init-root first)

Exit codes:
    0 = pass, device ready to ship
    1 = flash failed
    2 = file copy failed
    3 = config injection or key burn failed
    4 = serial verify failed
"""

import argparse
import os
import sys
import time
import json
import glob
import subprocess
import secrets
import uuid

# Resolve paths relative to this script (lives in backend/).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.join(SCRIPT_DIR, "..", "firmware")
DEFAULT_UF2 = os.path.join(FIRMWARE_DIR, "micropython-pico2.uf2")

def find_pico_serial():
    """Find the Pico's serial port. Prefers /dev/ttyACM* but falls back
    to mpremote auto-discovery (handles USB CDC + mass-storage modes)."""
    candidates = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    if candidates:
        return candidates[0]
    try:
        r = subprocess.run(["mpremote", "exec", "print('ok')"],
                            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and "ok" in r.stdout:
            return "auto"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def mpremote(port, *args, timeout=15):
    """Run an mpremote command on the given port."""
    if port == "auto":
        cmd = ["mpremote"] + list(args)
    else:
        cmd = ["mpremote", "connect", port] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            print(f"mpremote error: {result.stderr.strip()}")
        return result
    except subprocess.TimeoutExpired:
        print(f"mpremote timeout: {' '.join(cmd)}")
        return None


def wait_for_serial(timeout=30):
    """Wait for the Pico to appear as a serial port (after reboot)."""
    print(f"Waiting for Pico serial port... Timeout {timeout}s")
    start = time.time()
    while time.time() - start < timeout:
        port = find_pico_serial()
        if port:
            print(f"Found Pico at {port}")
            return port
        time.sleep(0.5)
    return None


def copy_firmware(port):
    """Copy main.py and lib/ to the device."""
    print("Copying firmware to device...")
    # Copy main.py
    main_py = os.path.join(FIRMWARE_DIR, "main.py")
    if not os.path.exists(main_py):
        print(f"ERROR: main.py not found at {main_py}")
        return False
    r = mpremote(port, "fs", "cp", main_py, ":main.py")
    if not r or r.returncode != 0:
        return False

    # Copy lib/ directory
    lib_dir = os.path.join(FIRMWARE_DIR, "lib")
    if not os.path.isdir(lib_dir):
        print(f"ERROR: lib/ not found at {lib_dir}")
        return False

    # Create lib dir on device and copy each file
    mpremote(port, "fs", "mkdir", ":lib")
    for fname in os.listdir(lib_dir):
        fpath = os.path.join(lib_dir, fname)
        if os.path.isfile(fpath) and not fname.startswith("__"):
            print(f"  copying {fname}")
            r = mpremote(port, "fs", "cp", fpath, f":lib/{fname}")
            if not r or r.returncode != 0:
                print(f"  failed to copy {fname}")
                return False

    print("Firmware copied.")
    return True


def inject_config(port, ssid=None, wifi_pass=None):
    """Write config.json to the device.

    Always writes a fresh device_token. If ssid and wifi_pass are provided,
    they are written so the device can boot straight into monitoring mode
    without going through the PWA. Device identity (private key, cert) is
    injected separately via key burn.
    """
    config = {
        "device_token": secrets.token_hex(32),
    }
    if ssid:
        config["ssid"] = ssid
    if wifi_pass:
        config["pass"] = wifi_pass
    config_json = json.dumps(config)
    print(f"Injecting config.json (device token{'+ Wi-Fi' if ssid else ''})...")
    escaped = config_json.replace("'", "\\'")
    r = mpremote(port, "exec",
                 f"open('/config.json','w').write('{escaped}')",
                 timeout=10)
    if not r or r.returncode != 0:
        return False
    print("Config injected.")
    return True


def load_update_public_key():
    """Load the 65-byte raw update public key for device burn."""
    keygen_path = os.path.join(SCRIPT_DIR, "keygen.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("keygen", keygen_path)
    keygen_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(keygen_mod)
    bin_path = keygen_mod.UPDATE_PUBLIC_BIN_PATH
    if not os.path.exists(bin_path):
        print(f"ERROR: Update public key not found at {bin_path}")
        print("Run: python keygen.py --init-update")
        return None
    with open(bin_path, "rb") as f:
        pub = f.read()
    if len(pub) != 65:
        print(f"ERROR: Update public key must be 65 bytes, got {len(pub)}")
        return None
    return pub


def burn_device_keys(port, device_keys):
    # burn the cryptographic device keys into the Pico's key storage.
    # calls otp_keys.burn_keys() on the device via mpremote exec, then
    # performs a separate read-back verify to confirm what landed in
    # storage matches what we intended to burn.
    priv_hex = device_keys['private_key'].hex()
    pub_hex = device_keys['public_key'].hex()
    serial_hex = device_keys['serial'].hex()
    cert_hex = device_keys['certificate'].hex()
    expected_serial = device_keys['serial_hex']

    print(f"Burning device keys (serial={serial_hex})...")

    # step 1: confirm the device is using the OTP backend, not the flash
    # fallback. a device that ended up on the flash path cannot ship:
    # its key is readable via BOOTSEL mass storage.
    r = mpremote(port, "exec",
                 "import otp_keys; print(otp_keys._BACKEND)",
                 timeout=10)
    if not r or r.returncode != 0:
        print(f"Backend check failed: {r.stderr if r else 'no response'}")
        return False
    backend = r.stdout.strip().strip("'").strip('"')
    if backend != "otp":
        print(f"ERROR: device backend is '{backend}', not 'otp'.")
        print("  The flash fallback is for development only -- it is")
        print("  readable via BOOTSEL. Do not ship a device on the flash")
        print("  backend. Re-flash a MicroPython build that exposes")
        print("  machine.OTP and re-run flash_known.py.")
        return False
    print(f"  backend: {backend}")

    # step 2: refuse to re-burn a device that already has keys. OTP is
    # one-time-write and we cannot tell apart "burned today" from
    # "burned last month" -- the safe move is to fail loudly.
    r = mpremote(port, "exec",
                 "import otp_keys; print(otp_keys.has_keys())",
                 timeout=10)
    if not r or r.returncode != 0:
        print(f"Pre-burn check failed: {r.stderr if r else 'no response'}")
        return False
    if r.stdout.strip().strip("'").strip('"').lower() == "true":
        print("ERROR: device already has keys burned. OTP is one-time-write.")
        print("  Use a fresh Pico, or accept that this device cannot be")
        print("  re-keyed. Its current keys remain valid -- it is just")
        print("  not the keys we just generated.")
        return False

    # step 3: burn.
    exec_code = (
        f"import otp_keys; "
        f"otp_keys.burn_keys("
        f"bytes.fromhex('{priv_hex}'), "
        f"bytes.fromhex('{pub_hex}'), "
        f"bytes.fromhex('{serial_hex}'), "
        f"bytes.fromhex('{cert_hex}'))"
    )
    r = mpremote(port, "exec", exec_code, timeout=15)
    if not r or r.returncode != 0:
        print(f"Key burn failed: {r.stderr if r else 'no response'}")
        return False

    # step 4: separate read-back verify. burn_keys() does its own
    # internal verify-back; this is a defense-in-depth check from
    # outside the burn function, so a bug in burn_keys cannot silently
    # pass a bad burn.
    if not verify_burn(port, device_keys):
        return False

    print("Device keys burned and verified.")
    return True


def burn_update_key_only(port, update_pub):
    """Burn only the firmware-update public key into an already-provisioned device.

    This is the migration path for devices that were manufactured before the
    update-key system existed. The device identity (attestation keys) is left
    untouched.
    """
    print("Burning update public key into already-provisioned device...")

    # Must be on OTP backend (same safety rule as device keys).
    r = mpremote(port, "exec",
                 "import otp_keys; print(otp_keys._BACKEND)",
                 timeout=10)
    if not r or r.returncode != 0:
        print(f"Backend check failed: {r.stderr if r else 'no response'}")
        return False
    backend = r.stdout.strip().strip("'").strip('"')
    if backend != "otp":
        print(f"ERROR: device backend is '{backend}', not 'otp'.")
        print("  Re-flash a MicroPython build that exposes machine.OTP.")
        return False
    print(f"  backend: {backend}")

    # Refuse if update key already present.
    r = mpremote(port, "exec",
                 "import otp_keys; print(otp_keys.has_update_key())",
                 timeout=10)
    if not r or r.returncode != 0:
        print(f"Pre-burn check failed: {r.stderr if r else 'no response'}")
        return False
    if r.stdout.strip().lower() == "true":
        print("ERROR: device already has an update key burned.")
        print("  OTP is one-time-write. Use a fresh Pico or accept the existing key.")
        return False

    pub_hex = update_pub.hex()
    exec_code = (
        f"import otp_keys; "
        f"print(otp_keys.burn_update_key(bytes.fromhex('{pub_hex}'), 0))"
    )
    r = mpremote(port, "exec", exec_code, timeout=15)
    if not r or r.returncode != 0:
        print(f"Update key burn failed: {r.stderr if r else 'no response'}")
        return False
    if r.stdout.strip().lower() != "true":
        print("ERROR: burn_update_key returned False on device")
        return False

    # read-back verify
    r = mpremote(port, "exec",
                 "import otp_keys; print(otp_keys.has_update_key())",
                 timeout=10)
    if not r or r.returncode != 0 or r.stdout.strip().lower() != "true":
        print("ERROR: read-back verify failed: has_update_key is False")
        return False
    r = mpremote(port, "exec",
                 "import ubinascii, otp_keys; "
                 "v = otp_keys.get_update_pubkey(); "
                 "print(ubinascii.hexlify(v).decode() if v else '')",
                 timeout=10)
    if not r or r.returncode != 0:
        print("ERROR: read-back verify failed: could not read update pubkey")
        return False
    if r.stdout.strip() != pub_hex:
        print("ERROR: read-back verify failed: update pubkey mismatch")
        return False

    print("Update key burned and verified.")
    return True


def verify_burn(port, device_keys):
    # read every field back from the device and compare byte-for-byte
    # against the keys we intended to burn. this is the last line of
    # defense before the device ships.
    fields = [
        # (label, attr, expected_string)
        # private_key/public_key/certificate are bytes -- the device
        # hex-encodes them for transmission. serial is already a hex
        # string on the device, returned as-is.
        ("private", "private_key", device_keys['private_key'].hex()),
        ("public", "public_key", device_keys['public_key'].hex()),
        ("serial", "serial", device_keys['serial_hex']),
        ("certificate", "certificate", device_keys['certificate'].hex()),
    ]

    for label, attr, expected_str in fields:
        # read back the field and ask the device to print it as hex
        # (or, for serial which is already a hex string, just print it).
        if attr == "serial":
            script = (f"import otp_keys; "
                      f"v = otp_keys.get_serial(); "
                      f"print(v if v else '')")
        else:
            script = (f"import ubinascii, otp_keys; "
                      f"v = otp_keys.get_{attr}(); "
                      f"print(ubinascii.hexlify(v).decode() if v else '')")
        r = mpremote(port, "exec", script, timeout=10)
        if not r or r.returncode != 0:
            print(f"  read-back of {label} failed: "
                  f"{r.stderr if r else 'no response'}")
            return False
        actual = r.stdout.strip()
        if actual != expected_str:
            print(f"  read-back mismatch on {label}:")
            print(f"    expected: {expected_str[:80]}{'...' if len(expected_str) > 80 else ''}")
            print(f"    got:      {actual[:80]}{'...' if len(actual) > 80 else ''}")
            return False

    # has_keys() must be true after the burn
    r = mpremote(port, "exec",
                 "import otp_keys; print(otp_keys.has_keys())",
                 timeout=10)
    if not r or r.returncode != 0:
        print(f"  has_keys check failed: {r.stderr if r else 'no response'}")
        return False
    if r.stdout.strip().lower() != "true":
        print("  read-back: has_keys() is False after burn")
        return False

    print("  read-back verify: all fields match")
    return True


def serial_verify(port):
    """Send the identify command and verify keys are present."""
    print("Serial verify (identify + key check)...")
    r = mpremote(port, "exec",
                 "import otp_keys, json; "
                 "print(json.dumps({'status':'ok',"
                 "'serial':otp_keys.get_serial(),"
                 "'has_keys':otp_keys.has_keys()}))",
                 timeout=10)
    if not r or r.returncode != 0:
        print("Serial verify failed: no response")
        return False
    try:
        resp = json.loads(r.stdout.strip().strip("'"))
        if resp.get("status") != "ok":
            print(f"Serial verify failed: bad status ({resp})")
            return False
        if not resp.get("has_keys"):
            print("Serial verify failed: keys not present after burn")
            return False
        serial = resp.get("serial", "")
        print(f"Serial verify passed: serial={serial}")
        return True
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Serial verify failed: couldn't parse response ({e})")
        print(f"  raw: {r.stdout}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Flash + configure a Heron device")
    parser.add_argument("--uf2", default=DEFAULT_UF2,
                        help=f"Path to MicroPython UF2 (default: {DEFAULT_UF2})")
    parser.add_argument("--skip-uf2", action="store_true",
                        help="Skip UF2 flashing (device already has MicroPython)")
    parser.add_argument("--ssid", default=None,
                        help="Wi-Fi SSID to write into /config.json (default: none)")
    parser.add_argument("--pass", dest="wifi_pass", default=None,
                        help="Wi-Fi password to write into /config.json (default: none)")
    parser.add_argument("--burn-update-key", action="store_true",
                        help="Burn only the update key into an already-provisioned device (migration)")
    args = parser.parse_args()

    if args.burn_update_key:
        # Migration path: device already has identity keys; just add update key.
        update_pub = load_update_public_key()
        if update_pub is None:
            sys.exit(1)
        port = wait_for_serial()
        if not port:
            print("ERROR: Pico serial port not found. Unplug and replug (without BOOTSEL).")
            sys.exit(1)
        if not burn_update_key_only(port, update_pub):
            print("ERROR: Update key burn failed.")
            sys.exit(3)
        print()
        print("=" * 50)
        print("  UPDATE KEY BURNED")
        print("=" * 50)
        print()
        return

    # Step 1: Generate device cryptographic keys
    from keygen import generate_device
    device_keys = generate_device()
    print(f"Generated device keys: serial={device_keys['serial_hex']}")

    # Step 2: Flash UF2 (or skip)
    if not args.skip_uf2:
        print("ERROR: UF2 flashing requires mass-storage BOOTSEL mode,")
        print("  which is not available on this system. Flash the UF2")
        print("  manually by copying it to the Pico's mass storage drive,")
        print("  then re-run with --skip-uf2.")
        sys.exit(1)

    # Step 3: Wait for serial port
    port = wait_for_serial()
    if not port:
        print("ERROR: Pico serial port not found. Unplug and replug (without BOOTSEL).")
        sys.exit(1)

    # Step 4: Copy firmware
    if not copy_firmware(port):
        print("ERROR: Firmware copy failed.")
        sys.exit(2)

    # Step 5: Inject config (device token only).
    #
    # sticker_code was removed 2026-06-20 when crypto attestation replaced
    # the old sticker-code activation system. The device is now authenticated
    # via ECDSA P-256 challenge-response (see keygen.py + otp_keys.py).
    #
    # Existing devices that still have sticker_code in their config.json
    # will lose it naturally on next re-provision. No migration script needed
    # -- the old key is harmless and the firmware doesn't read it.
    if not inject_config(port, ssid=args.ssid, wifi_pass=args.wifi_pass):
        print("ERROR: Config injection failed.")
        sys.exit(3)

    # Step 6: Burn cryptographic keys
    if not burn_device_keys(port, device_keys):
        print("ERROR: Key burn failed.")
        sys.exit(3)

    # Step 7: Burn firmware-update public key
    update_pub = load_update_public_key()
    if update_pub is None:
        print("ERROR: Could not load update public key.")
        sys.exit(3)
    if not burn_update_key_only(port, update_pub):
        print("ERROR: Update key burn failed.")
        sys.exit(3)

    # Step 8: Serial verify
    if not serial_verify(port):
        print("ERROR: Serial verification failed.")
        sys.exit(4)

    # Done
    print()
    print("=" * 50)
    print("  DEVICE READY TO SHIP")
    print(f"  Serial:  {device_keys['serial_hex']}")
    print("=" * 50)
    print()

if __name__ == "__main__":
    main()
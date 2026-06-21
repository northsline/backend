#!/usr/bin/env python3
"""
flash_known.py — one-command manufacturing script for Known.

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
FIRMWARE_DIR = os.path.join(SCRIPT_DIR, "..", "known", "firmware")
DEFAULT_UF2 = os.path.join(FIRMWARE_DIR, "micropython-pico2.uf2")

import db


def find_pico_serial():
    """Find the Pico's serial port (/dev/ttyACMx on Linux)."""
    candidates = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    if not candidates:
        return None
    # If multiple, prefer the first — user should only have one plugged in.
    return candidates[0]


def find_pico_mount():
    """Find the Pico's mass storage mount point (BOOTSEL mode)."""
    # On Linux, the Pico shows up as a block device when in BOOTSEL mode.
    # Check mounted filesystems for something that looks like a Pico.
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mount_point = parts[1]
                    # Pico mounts with a FAT filesystem, usually labeled "RPI-RP2"
                    if "RPI-RP" in mount_point.upper() or "RP2" in mount_point.upper():
                        return mount_point
        # Fallback: check common mount points
        for d in ["/run/media", "/media"]:
            for root, dirs, _ in os.walk(d):
                for dir_name in dirs:
                    full = os.path.join(root, dir_name)
                    if "RPI" in dir_name.upper() or "PICO" in dir_name.upper():
                        return full
    except Exception:
        pass
    return None


def wait_for_bootsel(timeout=120):
    """Wait for the Pico to appear in BOOTSEL mode (mass storage)."""
    print(f"Waiting for Pico in BOOTSEL mode (hold BOOTSEL, plug in USB)... "
          f"timeout {timeout}s")
    start = time.time()
    while time.time() - start < timeout:
        mount = find_pico_mount()
        if mount:
            print(f"Found Pico at {mount}")
            return mount
        time.sleep(0.5)
    return None


def wait_for_serial(timeout=30):
    """Wait for the Pico to appear as a serial port (after reboot)."""
    print(f"Waiting for Pico serial port... timeout {timeout}s")
    start = time.time()
    while time.time() - start < timeout:
        port = find_pico_serial()
        if port:
            print(f"Found Pico at {port}")
            return port
        time.sleep(0.5)
    return None


def flash_uf2(uf2_path, mount_point):
    """Copy the UF2 file to the Pico's mass storage to flash it."""
    if not os.path.exists(uf2_path):
        print(f"ERROR: UF2 file not found at {uf2_path}")
        print("Download from https://micropython.org/download/RPI-PICO2-W/")
        return False
    dest = os.path.join(mount_point, "firmware.uf2")
    print(f"Flashing {os.path.basename(uf2_path)} to {mount_point}...")
    try:
        subprocess.run(["cp", uf2_path, dest], check=True)
        # Pico reboots automatically after UF2 copy
        print("UF2 copied. Pico will reboot...")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Flash failed: {e}")
        return False


def mpremote(port, *args, timeout=15):
    """Run an mpremote command on the given port."""
    cmd = ["mpremote", "connect", port] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            print(f"mpremote error: {result.stderr.strip()}")
        return result
    except subprocess.TimeoutExpired:
        print(f"mpremote timeout: {' '.join(cmd)}")
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


def inject_config(port):
    """Write config.json to the device with Wi-Fi placeholder.
    Device identity (private key, cert) is injected separately via key burn."""
    config = {
        "device_token": secrets.token_hex(32),
    }
    config_json = json.dumps(config)
    print(f"Injecting config.json (device token)...")
    escaped = config_json.replace("'", "\\'")
    r = mpremote(port, "exec",
                 f"open('/config.json','w').write('{escaped}')",
                 timeout=10)
    if not r or r.returncode != 0:
        return False
    print("Config injected.")
    return True


def burn_device_keys(port, device_keys):
    """Burn the cryptographic device keys into the Pico's key storage.

    Calls otp_keys.burn_keys() on the device via mpremote exec.
    """
    priv_hex = device_keys['private_key'].hex()
    pub_hex = device_keys['public_key'].hex()
    serial_hex = device_keys['serial'].hex()
    cert_hex = device_keys['certificate'].hex()

    print(f"Burning device keys (serial={serial_hex})...")
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
    print("Device keys burned successfully.")
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
    parser = argparse.ArgumentParser(description="Flash + configure a Known device")
    parser.add_argument("--uf2", default=DEFAULT_UF2,
                        help=f"Path to MicroPython UF2 (default: {DEFAULT_UF2})")
    parser.add_argument("--skip-uf2", action="store_true",
                        help="Skip UF2 flashing (device already has MicroPython)")
    args = parser.parse_args()

    # Step 1: Generate device cryptographic keys
    from keygen import generate_device
    device_keys = generate_device()
    print(f"Generated device keys: serial={device_keys['serial_hex']}")

    # Step 2: Flash UF2 (or skip)
    if not args.skip_uf2:
        mount = wait_for_bootsel()
        if not mount:
            print("ERROR: Pico not found in BOOTSEL mode.")
            sys.exit(1)
        if not flash_uf2(args.uf2, mount):
            sys.exit(1)
        print("Waiting for Pico to reboot after flash...")
        time.sleep(3)

    # Step 3: Wait for serial port
    port = wait_for_serial()
    if not port:
        print("ERROR: Pico serial port not found. Unplug and replug (without BOOTSEL).")
        sys.exit(1)

    # Step 4: Copy firmware
    if not copy_firmware(port):
        print("ERROR: Firmware copy failed.")
        sys.exit(2)

    # Step 5: Inject config (device token only, no sticker code)
    if not inject_config(port):
        print("ERROR: Config injection failed.")
        sys.exit(3)

    # Step 6: Burn cryptographic keys
    if not burn_device_keys(port, device_keys):
        print("ERROR: Key burn failed.")
        sys.exit(3)

    # Step 7: Serial verify
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

    # Generate sticker automatically
    try:
        import sticker
        sticker_path = os.path.join(sticker.STICKER_DIR,
                                     f"{device_keys['serial_hex']}.png")
        sticker_img = sticker.generate_sticker_image(device_keys['serial_hex'])
        os.makedirs(sticker.STICKER_DIR, exist_ok=True)
        sticker_img.save(sticker_path, "PNG")
        print(f"Sticker saved:  {sticker_path}")
        print("Print it and attach to the device.")
    except Exception as e:
        print(f"Sticker generation skipped (run: python sticker.py {device_keys['serial_hex']})")
        print(f"  reason: {e}")


if __name__ == "__main__":
    main()
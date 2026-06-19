#!/usr/bin/env python3
"""
flash_known.py — one-command manufacturing script for Known.

Does the full per-device setup in one shot:
  1. Generate (or fetch) a sticker code + device credentials
  2. Wait for Pico in BOOTSEL mode, flash MicroPython UF2
  3. Copy firmware (main.py + lib/) to the device
  4. Inject /config.json with sticker_code, device_secret, device_id
  5. Serial verify with the identify command
  6. Print the sticker code for labeling

Usage:
    python flash_known.py                    # generate a new code, flash, verify
    python flash_known.py --code KNOWN-ABCD-1234   # use a pre-registered code
    python flash_known.py --skip-uf2         # skip UF2 flash (already flashed)

Requires:
    - mpremote (pip install mpremote)
    - Pico 2 W connected via USB
    - MicroPython UF2 file at ../firmware/micropython-pico2.uf2 (or pass --uf2)
    - codes.db in backend/ (for code registration)

Exit codes:
    0 = pass, device ready to ship
    1 = flash failed
    2 = file copy failed
    3 = config injection failed
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


def inject_config(port, sticker_code, device_secret, device_id):
    """Write config.json to the device with the device credentials."""
    config = {
        "sticker_code": sticker_code,
        "device_id": device_id,
    }
    config_json = json.dumps(config)
    print(f"Injecting config.json (code={sticker_code}, id={device_id[:8]}...)...")
    # Use exec to write the file — more reliable than fs cp from a temp file
    escaped = config_json.replace("'", "\\'")
    r = mpremote(port, "exec",
                 f"open('/config.json','w').write('{escaped}')",
                 timeout=10)
    if not r or r.returncode != 0:
        return False
    # Verify it was written
    r = mpremote(port, "exec",
                 "print(open('/config.json').read())",
                 timeout=10)
    if not r or r.returncode != 0:
        return False
    try:
        written = json.loads(r.stdout.strip().strip("'"))
        if written.get("sticker_code") != sticker_code:
            print("Config verification failed: sticker_code mismatch")
            return False
    except (json.JSONDecodeError, ValueError):
        print("Config verification failed: couldn't parse response")
        return False
    print("Config injected and verified.")
    return True


def serial_verify(port):
    """Send the identify command and check the response."""
    print("Serial verify (identify)...")
    # The provisioning code listens for JSON commands over serial.
    # Send identify and check the response.
    r = mpremote(port, "exec",
                 "import provisioning; "
                 "cfg = provisioning.load_config(); "
                 "import json; "
                 "print(json.dumps({'status':'ok','code':cfg.get('sticker_code'),"
                 "'device_id':cfg.get('device_id')}))",
                 timeout=10)
    if not r or r.returncode != 0:
        print("Serial verify failed: no response")
        return False
    try:
        resp = json.loads(r.stdout.strip().strip("'"))
        if resp.get("status") != "ok":
            print(f"Serial verify failed: bad status ({resp})")
            return False
        if not resp.get("code"):
            print("Serial verify failed: no sticker code in config")
            return False
        print(f"Serial verify passed: code={resp['code']}")
        return True
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Serial verify failed: couldn't parse response ({e})")
        print(f"  raw: {r.stdout}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Flash + configure a Known device")
    parser.add_argument("--code", help="Use a pre-registered sticker code")
    parser.add_argument("--uf2", default=DEFAULT_UF2,
                        help=f"Path to MicroPython UF2 (default: {DEFAULT_UF2})")
    parser.add_argument("--skip-uf2", action="store_true",
                        help="Skip UF2 flashing (device already has MicroPython)")
    args = parser.parse_args()

    # Step 1: Get or create a sticker code + credentials
    db.init_db()
    if args.code:
        code = args.code.strip().upper()
        device = db.get_device(code)
        if not device:
            print(f"ERROR: code {code} not found in codes.db. Run generate_codes.py first.")
            sys.exit(1)
        if device["status"] == "claimed":
            print(f"ERROR: code {code} already claimed.")
            sys.exit(1)
        sticker_code = device["sticker_code"]
        device_secret = device["device_secret"]
        device_id = device["device_id"]
    else:
        # Generate a new code
        from generate_codes import generate_code
        sticker_code = generate_code()
        device_secret = secrets.token_hex(32)
        device_id = str(uuid.uuid4())
        db.insert_device(sticker_code, device_secret, device_id)
        print(f"Generated new code: {sticker_code}")

    # Step 2: Flash UF2 (or skip)
    if not args.skip_uf2:
        mount = wait_for_bootsel()
        if not mount:
            print("ERROR: Pico not found in BOOTSEL mode.")
            sys.exit(1)
        if not flash_uf2(args.uf2, mount):
            sys.exit(1)
        # Wait for the Pico to reboot after flash
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

    # Step 5: Inject config
    if not inject_config(port, sticker_code, device_secret, device_id):
        print("ERROR: Config injection failed.")
        sys.exit(3)

    # Step 6: Serial verify
    if not serial_verify(port):
        print("ERROR: Serial verification failed.")
        sys.exit(4)

    # Done
    print()
    print("=" * 50)
    print("  DEVICE READY TO SHIP")
    print(f"  Sticker code: {sticker_code}")
    print(f"  Device ID:    {device_id}")
    print("=" * 50)
    print()
    print("Print a sticker with the code above and attach to the device.")


if __name__ == "__main__":
    main()
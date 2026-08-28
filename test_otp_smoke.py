#!/usr/bin/env python3
"""
test_otp_smoke.py. Verify the custom MicroPython OTP C module on real silicon.

Flashes nothing. Writes a few bytes to an unused OTP scratch region, reads
them back, and reports. Safe to run repeatedly on a sacrificial Pico because
the scratch offset is far above the Heron key layout.

Usage:
    python test_otp_smoke.py        # auto-find Pico serial port
    python test_otp_smoke.py /dev/ttyACM0

Requires: mpremote, a Pico 2 W running the custom MicroPython UF2 with
machine.OTP support.
"""

import glob
import subprocess
import sys
import time

SCRATCH_OFFSET = 9000
# Use a 6-byte pattern that deliberately spans multiple rows and row
# boundaries. The C module must correctly pack 3 usable bytes per OTP
# row; a 6-byte write covers two full rows and tests the byte-to-row
# mapping at every position.
TEST_PATTERN = b'\xde\xad\xbe\xef\xca\xfe'


def find_pico_serial():
    candidates = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    return candidates[0] if candidates else None


def mpremote_exec(port, code, timeout=30):
    cmd = ["mpremote", "connect", port, "exec", code]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        print("ERROR: mpremote not found. Run: pip install mpremote")
        sys.exit(1)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_pico_serial()
    if not port:
        print("ERROR: No Pico serial port found. Plug it in (without BOOTSEL).")
        sys.exit(1)

    print(f"OTP smoke test on {port}")
    print(f"Scratch offset: {SCRATCH_OFFSET}")
    print()

    # First, confirm machine.OTP exists.
    r = mpremote_exec(port, "import machine; print(hasattr(machine, 'OTP'))")
    if not r or r.returncode != 0:
        print("FAIL: could not query machine.OTP")
        if r:
            print("stderr:", r.stderr.strip())
        sys.exit(1)
    has_otp = r.stdout.strip().lower() == "true"
    print(f"machine.OTP exists: {has_otp}")
    if not has_otp:
        print("FAIL: machine.OTP is not present. Flash the custom UF2 first.")
        sys.exit(1)

    # Run the smoke test on the device.
    test_code = (
        "import machine\n"
        f"otp = machine.OTP()\n"
        f"off = {SCRATCH_OFFSET}\n"
        f"pattern = bytes.fromhex('{TEST_PATTERN.hex()}')\n"
        f"existing = otp.read(off, 4)\n"
        f"otp.write(off, pattern)\n"
        f"read_back = otp.read(off, {len(TEST_PATTERN)})\n"
        "print('existing:', existing.hex() if existing else 'None')\n"
        "print('pattern:', pattern.hex())\n"
        "print('read_back:', read_back.hex() if read_back else 'None')\n"
        "print('PASS' if read_back == pattern else 'FAIL')\n"
    )

    print("Writing scratch pattern...")
    r = mpremote_exec(port, test_code, timeout=60)
    if not r or r.returncode != 0:
        print("FAIL: mpremote exec failed")
        if r:
            print("stderr:", r.stderr.strip())
        sys.exit(1)

    print(r.stdout.strip())
    print()
    if "PASS" in r.stdout:
        print("OTP smoke test PASSED.")
        print("You can now trust the OTP C module and proceed to key burning.")
    else:
        print("OTP smoke test FAILED.")
        print("Do not proceed. Debug otp.c before touching real keys.")
        sys.exit(1)


if __name__ == "__main__":
    main()

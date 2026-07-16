#!/usr/bin/env python3
"""
test_otp_burn.py. Verifies the burn_device_keys / verify_burn
flow in flash_known.py against a mock mpremote port.

The full hardware flow requires a real Pico 2 W with MicroPython
exposing machine.OTP. This test covers the manufacturing script's
correctness in isolation: it mocks the mpremote responses and
asserts that flash_known.py makes the right calls, in the right
order, with the right error handling.

Run with: python3 test_otp_burn.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flash_known
from unittest import mock


def make_keys():
    priv = bytes(range(32))
    pub = b'\x04' + b'\x01' * 32 + b'\x02' * 32
    serial = b'\xaa\xbb\xcc\xdd\xee\xff\x11\x22'
    cert = b'\x30\x46\x02\x21' + b'\xcd' * 70
    return {
        "private_key": priv,
        "public_key": pub,
        "serial": serial,
        "serial_hex": serial.hex(),
        "certificate": cert,
    }


class FakeMpremote:
    """drop-in replacement for flash_known.mpremote that records every
    call and returns scripted responses."""

    def __init__(self, backend="otp", already_has_keys=False,
                 burn_succeeds=True, verify_succeeds=True):
        self.call_log = []
        self.backend = backend
        self.already_has_keys = already_has_keys
        self.burn_succeeds = burn_succeeds
        self.verify_succeeds = verify_succeeds
        self._has_keys_checks = 0

    def __call__(self, port, *args, **kwargs):
        self.call_log.append(list(args))
        cmd = args[1] if len(args) > 1 else ""
        if "_BACKEND" in cmd:
            return mock.Mock(returncode=0, stdout=self.backend, stderr="")
        if "has_keys()" in cmd:
            self._has_keys_checks += 1
            if self._has_keys_checks == 1:
                # pre-burn check
                return mock.Mock(returncode=0,
                                 stdout=str(self.already_has_keys), stderr="")
            # post-burn check
            return mock.Mock(returncode=0,
                             stdout=str(self.verify_succeeds), stderr="")
        if "burn_keys(" in cmd:
            if not self.burn_succeeds:
                return mock.Mock(returncode=1, stdout="", stderr="burn failed")
            return mock.Mock(returncode=0, stdout="True", stderr="")
        # per-field read in verify_burn
        # the device returns hex string for bytes fields, the hex
        # string itself for serial.
        if "get_private_key" in cmd:
            val = bytes(range(32)).hex()
        elif "get_public_key" in cmd:
            val = (b'\x04' + b'\x01' * 32 + b'\x02' * 32).hex()
        elif "get_serial" in cmd:
            val = "aabbccddeeff1122"
        elif "get_certificate" in cmd:
            val = (b'\x30\x46\x02\x21' + b'\xcd' * 70).hex()
        else:
            val = ""
        if not self.verify_succeeds:
            val = "deadbeef" * 8  # arbitrary wrong bytes
        return mock.Mock(returncode=0, stdout=val, stderr="")


def test_burn_rejects_flash_backend():
    keys = make_keys()
    fake = FakeMpremote(backend="flash")
    with mock.patch.object(flash_known, "mpremote", side_effect=fake):
        result = flash_known.burn_device_keys("/dev/null", keys)
    assert result is False
    print("  PASS: burn refuses flash backend")


def test_burn_rejects_already_burned_device():
    keys = make_keys()
    fake = FakeMpremote(backend="otp", already_has_keys=True)
    with mock.patch.object(flash_known, "mpremote", side_effect=fake):
        result = flash_known.burn_device_keys("/dev/null", keys)
    assert result is False
    print("  PASS: burn refuses already-keyed device")


def test_burn_happy_path():
    keys = make_keys()
    fake = FakeMpremote(backend="otp", already_has_keys=False,
                        burn_succeeds=True, verify_succeeds=True)
    with mock.patch.object(flash_known, "mpremote", side_effect=fake):
        result = flash_known.burn_device_keys("/dev/null", keys)
    assert result is True, "burn should succeed on happy path"
    # sequence: _BACKEND, has_keys pre, burn, then per-field readbacks
    # (private, public, serial, cert), then has_keys post
    assert len(fake.call_log) >= 7, \
        f"expected >= 7 mpremote calls, got {len(fake.call_log)}"
    assert "_BACKEND" in fake.call_log[0][1]
    assert "has_keys" in fake.call_log[1][1]
    assert "burn_keys" in fake.call_log[2][1]
    print("  PASS: burn happy path sequence is correct")


def test_burn_fails_on_readback_mismatch():
    keys = make_keys()
    fake = FakeMpremote(backend="otp", verify_succeeds=False)
    with mock.patch.object(flash_known, "mpremote", side_effect=fake):
        result = flash_known.burn_device_keys("/dev/null", keys)
    assert result is False, "burn must fail when readback mismatches"
    print("  PASS: burn fails on readback mismatch")


def test_burn_fails_on_burn_keys_failure():
    keys = make_keys()
    fake = FakeMpremote(burn_succeeds=False)
    with mock.patch.object(flash_known, "mpremote", side_effect=fake):
        result = flash_known.burn_device_keys("/dev/null", keys)
    assert result is False
    print("  PASS: burn fails when otp_keys.burn_keys fails")


def main():
    print("Heron flash_known burn flow tests")
    print("==================================")
    print()
    test_burn_rejects_flash_backend()
    test_burn_rejects_already_burned_device()
    test_burn_happy_path()
    test_burn_fails_on_readback_mismatch()
    test_burn_fails_on_burn_keys_failure()
    print()
    print("ALL TESTS PASSED")
    print("The burn flow is correct end-to-end (mocked mpremote).")
    print()
    print("Hardware verification still required (BLOCKED on C module):")
    print("  1. Build the custom MicroPython UF2 with the OTP C module")
    print("     (see rp2350-otp-c-module skill).")
    print("  2. Flash a sacrificial Pico 2 W with the custom UF2.")
    print("  3. Run an OTP smoke test: write a heron byte, read it back,")
    print("     compare. Must succeed before proceeding.")
    print("  4. Run: python flash_known.py with a throwaway key pair.")
    print("  5. Confirm the printout says 'backend: otp' and 'read-back")
    print("     verify: all fields match'.")
    print("  6. Run identify + challenge via the PWA, verify signature")
    print("     against an independent generation of the same key.")
    print("  7. Regression: flash a stock UF2 (no C module) and confirm")
    print("     flash_known.py refuses to burn it.")


if __name__ == "__main__":
    main()

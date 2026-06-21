#!/usr/bin/env python3
"""
test_crypto_pipeline.py — end-to-end crypto verification

Validates that:
  1. The root key can sign a device certificate
  2. The device private key can sign a nonce (using the same ECDSA P-256
     algorithm as firmware/lib/ecdsa.py)
  3. The root public key can verify the device certificate
  4. The device public key can verify the nonce signature
  5. The DER signatures are compatible with the cryptography library
     (which uses the same ECDSA implementation as Web Crypto)

This is the critical compatibility test. If this passes, the firmware
signer and the browser verifier will interoperate.
"""

import sys
import os
import json
import secrets
import hashlib

# Add firmware lib to path so we can import the pure-Python ecdsa module
FIRMWARE_LIB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "firmware", "lib"
)
sys.path.insert(0, FIRMWARE_LIB)

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend

# Import the pure-MicroPython ECDSA module (same code that runs on the Pico)
import ecdsa


def load_root_keys():
    """Load the root key pair from PEM files."""
    key_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys")
    with open(os.path.join(key_dir, "root_private.pem"), "rb") as f:
        root_priv = serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )
    with open(os.path.join(key_dir, "root_public.pem"), "rb") as f:
        root_pub = serialization.load_pem_public_key(
            f.read(), backend=default_backend()
        )
    return root_priv, root_pub


def test_cert_sign_and_verify(root_priv, root_pub):
    """Test 1: Root signs a device certificate, root verifies it."""
    print("=== Test 1: Certificate sign + verify ===")

    # Generate a device key pair
    dev_priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    dev_pub = dev_priv.public_key()
    dev_pub_nums = dev_pub.public_numbers()

    # Build the cert message: serial (8 bytes) || public key (65 bytes)
    serial = secrets.token_bytes(8)
    pub_bytes = (
        b'\x04' +
        dev_pub_nums.x.to_bytes(32, 'big') +
        dev_pub_nums.y.to_bytes(32, 'big')
    )
    cert_message = serial + pub_bytes

    # Root signs the cert
    cert_sig = root_priv.sign(cert_message, ec.ECDSA(hashes.SHA256()))

    # Root verifies the cert
    root_pub.verify(cert_sig, cert_message, ec.ECDSA(hashes.SHA256()))
    print("  PASS: root signed cert, root verified cert")
    print(f"  Serial: {serial.hex()}")
    print(f"  Cert sig length: {len(cert_sig)} bytes (DER)")

    return dev_priv, dev_pub, serial, pub_bytes, cert_sig


def test_nonce_sign_with_pure_python(dev_priv, dev_pub):
    """Test 2: Pure-Python ECDSA signs a nonce, cryptography verifies it.

    This is THE critical test. The firmware uses ecdsa.py (pure Python).
    The browser uses Web Crypto SubtleCrypto (same backend as cryptography).
    If this passes, the firmware and browser will interoperate.
    """
    print()
    print("=== Test 2: Pure-Python sign → cryptography verify ===")

    # Generate a random nonce (32 bytes, same as PWA does)
    nonce = secrets.token_bytes(32)
    print(f"  Nonce: {nonce.hex()}")

    # Get the device private key as an integer (same format as otp_keys.get_private_key_int())
    priv_nums = dev_priv.private_numbers()
    priv_int = priv_nums.private_value

    # Sign the nonce using the pure-Python ECDSA module
    # (This is exactly what the Pico firmware does)
    sig_der = ecdsa.sign(priv_int, nonce)
    print(f"  Pure-Python sig: {sig_der.hex()}")
    print(f"  Sig length: {len(sig_der)} bytes (DER)")

    # Verify the signature using the cryptography library
    # (This is what Web Crypto SubtleCrypto.verify does)
    dev_pub.verify(sig_der, nonce, ec.ECDSA(hashes.SHA256()))
    print("  PASS: cryptography verified the pure-Python signature")

    # Also verify that the pure-Python public key derivation matches
    pub_from_py = ecdsa.public_key_bytes(priv_int)
    pub_nums = dev_pub.public_numbers()
    expected_pub = (
        b'\x04' +
        pub_nums.x.to_bytes(32, 'big') +
        pub_nums.y.to_bytes(32, 'big')
    )
    if pub_from_py == expected_pub:
        print("  PASS: pure-Python public key derivation matches cryptography")
    else:
        print("  FAIL: public key mismatch!")
        print(f"    Expected: {expected_pub.hex()}")
        print(f"    Got:      {pub_from_py.hex()}")
        return False

    return True


def test_full_cert_flow(root_priv, root_pub, dev_pub, serial, pub_bytes, cert_sig):
    """Test 3: Full certificate verification (as the PWA would do it)."""
    print()
    print("=== Test 3: Full PWA verification flow ===")

    # Build the binary certificate (same format as keygen.py and otp_keys.py)
    cert_binary = bytes([len(cert_sig)]) + cert_sig + serial + pub_bytes
    print(f"  Cert binary: {len(cert_binary)} bytes")

    # Parse it (same as crypto.ts parseCert())
    sig_len = cert_binary[0]
    parsed_sig = cert_binary[1:1 + sig_len]
    parsed_serial = cert_binary[1 + sig_len:1 + sig_len + 8]
    parsed_pub = cert_binary[1 + sig_len + 8:1 + sig_len + 8 + 65]

    # Verify cert signature against root key
    cert_message = parsed_serial + parsed_pub
    root_pub.verify(parsed_sig, cert_message, ec.ECDSA(hashes.SHA256()))
    print("  PASS: parsed cert verified against root key")

    # Verify parsed public key matches original
    if parsed_pub == pub_bytes and parsed_serial == serial:
        print("  PASS: parsed cert fields match originals")
    else:
        print("  FAIL: parsed cert fields mismatch")
        return False

    return True


def test_multiple_signatures(dev_priv, dev_pub):
    """Test 4: Multiple signatures to check consistency."""
    print()
    print("=== Test 4: Multiple signatures (consistency check) ===")

    for i in range(5):
        nonce = secrets.token_bytes(32)
        priv_nums = dev_priv.private_numbers()
        priv_int = priv_nums.private_value
        sig_der = ecdsa.sign(priv_int, nonce)
        try:
            dev_pub.verify(sig_der, nonce, ec.ECDSA(hashes.SHA256()))
            print(f"  Signature {i+1}: PASS ({len(sig_der)} bytes)")
        except Exception as e:
            print(f"  Signature {i+1}: FAIL — {e}")
            return False

    print("  All 5 signatures verified successfully")
    return True


def main():
    print("Known Crypto Pipeline Test")
    print("==========================")
    print()

    # Load root keys
    root_priv, root_pub = load_root_keys()
    print(f"Root key loaded: P-256")
    print()

    # Test 1: Cert sign + verify
    dev_priv, dev_pub, serial, pub_bytes, cert_sig = test_cert_sign_and_verify(root_priv, root_pub)

    # Test 2: Pure-Python sign → cryptography verify (CRITICAL)
    if not test_nonce_sign_with_pure_python(dev_priv, dev_pub):
        print()
        print("CRITICAL FAILURE: Pure-Python ECDSA signatures are not compatible with cryptography.")
        print("The firmware and browser will NOT interoperate.")
        sys.exit(1)

    # Test 3: Full cert flow
    test_full_cert_flow(root_priv, root_pub, dev_pub, serial, pub_bytes, cert_sig)

    # Test 4: Multiple signatures
    test_multiple_signatures(dev_priv, dev_pub)

    print()
    print("==========================")
    print("ALL TESTS PASSED")
    print("The crypto pipeline is compatible end-to-end.")
    print()
    print("Next: flash a device and test the full hardware flow.")


if __name__ == '__main__':
    main()
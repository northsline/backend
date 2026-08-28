#!/usr/bin/env python3
"""
test_ecdsa_verify.py. Verify that the pure-Python ECDSA module (ecdsa.py)
can VERIFY signatures produced by the cryptography library.

This is the reverse direction of test_crypto_pipeline.py:
  - test_crypto_pipeline.py tests: sign(pure-python) -> verify(cryptography)
  - This test verifies:        sign(cryptography) -> verify(pure-python)

The firmware update system requires the Pico to verify bundle signatures
using ecdsa.py. This test proves that works.
"""

import sys
import os
import hashlib
import secrets
import tempfile
import json

FIRMWARE_LIB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "firmware", "lib"
)
sys.path.insert(0, FIRMWARE_LIB)

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend

import ecdsa

KEY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys")
UPDATE_PRIVATE_PATH = os.path.join(KEY_DIR, "update_private.pem")
UPDATE_PUBLIC_PATH = os.path.join(KEY_DIR, "update_public.pem")

passed = 0
failed = 0


def test(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1


def load_update_keys():
    with open(UPDATE_PRIVATE_PATH, "rb") as f:
        priv = serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )
    with open(UPDATE_PUBLIC_PATH, "rb") as f:
        pub = serialization.load_pem_public_key(
            f.read(), backend=default_backend()
        )
    return priv, pub


def pubkey_to_bytes(pub):
    nums = pub.public_numbers()
    return b'\x04' + nums.x.to_bytes(32, 'big') + nums.y.to_bytes(32, 'big')


def test_single_signature(priv, pub, pub_bytes):
    """Test 1: cryptography signs, pure-Python verifies."""
    print("=== Test 1: cryptography sign -> pure-Python verify ===")
    message = b"Heron firmware update v2.0.0"
    sig_der = priv.sign(message, ec.ECDSA(hashes.SHA256()))
    result = ecdsa.verify(pub_bytes, sig_der, message)
    test("single signature verify", result is True)
    print(f"  Message: {message.decode()}")
    print(f"  Signature: {len(sig_der)} bytes DER")
    return True


def test_multiple_messages(priv, pub_bytes):
    """Test 2: Multiple messages, all must verify."""
    print()
    print("=== Test 2: Multiple messages ===")
    messages = [
        b"hello",
        b"",
        b"a" * 1000,
        secrets.token_bytes(64),
        b"Heron firmware bundle v1.0.0",
        b"Heron firmware bundle v2.0.0",
        b"Heron firmware bundle v3.0.0",
    ]
    for i, msg in enumerate(messages):
        sig_der = priv.sign(msg, ec.ECDSA(hashes.SHA256()))
        result = ecdsa.verify(pub_bytes, sig_der, msg)
        test(f"message {i+1} ({len(msg)} bytes)", result is True)
    return True


def test_tampered_signature(priv, pub_bytes):
    """Test 3: Tampered signatures must be rejected."""
    print()
    print("=== Test 3: Tampered signature rejection ===")
    message = b"tamper test message"

    sig_der = priv.sign(message, ec.ECDSA(hashes.SHA256()))

    # Flip a bit in the signature
    bad_sig = bytearray(sig_der)
    bad_sig[10] ^= 0x01
    result = ecdsa.verify(pub_bytes, bytes(bad_sig), message)
    test("flipped bit in signature rejected", result is False)

    # Truncated signature
    result = ecdsa.verify(pub_bytes, sig_der[:10], message)
    test("truncated signature rejected", result is False)

    # Empty signature
    result = ecdsa.verify(pub_bytes, b"", message)
    test("empty signature rejected", result is False)

    # Garbage bytes
    result = ecdsa.verify(pub_bytes, b"\x00" * 72, message)
    test("garbage signature rejected", result is False)

    return True


def test_wrong_public_key(priv, pub_bytes):
    """Test 4: Wrong public key must reject valid signature."""
    print()
    print("=== Test 4: Wrong public key rejection ===")
    message = b"wrong key test"

    sig_der = priv.sign(message, ec.ECDSA(hashes.SHA256()))

    # Generate a different key pair
    other_priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    other_pub = other_priv.public_key()
    other_pub_bytes = pubkey_to_bytes(other_pub)

    result = ecdsa.verify(other_pub_bytes, sig_der, message)
    test("wrong public key rejected", result is False)

    # Wrong message with correct key
    result = ecdsa.verify(pub_bytes, sig_der, b"different message")
    test("wrong message rejected", result is False)

    # Malformed public key (too short)
    result = ecdsa.verify(b"\x04" + b"\x00" * 32, sig_der, message)
    test("malformed public key (too short) rejected", result is False)

    # Malformed public key (wrong prefix)
    result = ecdsa.verify(b"\x03" + b"\x00" * 64, sig_der, message)
    test("malformed public key (compressed prefix) rejected", result is False)

    return True


def test_roundtrip(priv, pub_bytes):
    """Test 5: Round-trip: pure-Python sign -> pure-Python verify."""
    print()
    print("=== Test 5: Pure-Python sign -> pure-Python verify (round-trip) ===")
    priv_nums = priv.private_numbers()
    priv_int = priv_nums.private_value

    for i in range(5):
        msg = secrets.token_bytes(32)
        sig_der = ecdsa.sign(priv_int, msg)
        result = ecdsa.verify(pub_bytes, sig_der, msg)
        test(f"round-trip {i+1}", result is True)

    return True


def test_bundle_signature(priv, pub_bytes):
    """Test 6: Verify a real .heronpkg bundle signature with ecdsa.py."""
    print()
    print("=== Test 6: Real bundle signature verification ===")

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "heron-cli"))
    from heron_cli import bundle

    # Build a minimal bundle
    files = {
        "main.py": b"# test firmware\nprint('hello')",
        "lib/test.py": b"# test lib\n",
    }
    pkg = bundle.build_bundle(
        files=files,
        fw_version=1,
        min_from=1,
        private_key=priv,
    )

    # Parse the bundle header to extract signature and signed digest
    offset = 0
    magic = pkg[offset:offset + 4]
    test("bundle magic is HRNP", magic == b"HRNP")
    offset += 4

    version = pkg[offset]
    test("bundle version is 0x01", version == 0x01)
    offset += 1

    sig_len = pkg[offset]
    offset += 1

    sig_der = pkg[offset:offset + sig_len]
    offset += sig_len

    manifest_len = int.from_bytes(pkg[offset:offset + 2], "little")
    offset += 2

    manifest_bytes = pkg[offset:offset + manifest_len]
    offset += manifest_len

    files_blob = pkg[offset:]

    signed_digest = hashlib.sha256(manifest_bytes + files_blob).digest()

    # Verify with ecdsa.py
    result = ecdsa.verify(pub_bytes, sig_der, signed_digest)
    test("bundle signature verified by ecdsa.py", result is True)

    # Tamper the signed digest
    bad_digest = bytearray(signed_digest)
    bad_digest[0] ^= 0xFF
    result = ecdsa.verify(pub_bytes, sig_der, bytes(bad_digest))
    test("tampered bundle digest rejected", result is False)

    return True


def test_der_roundtrip():
    """Test 7: DER encode -> decode round-trip."""
    print()
    print("=== Test 7: DER encode/decode round-trip ===")
    import ecdsa as ecdsa_mod

    test_cases = [
        (1, 1),
        (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
         0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5),
        (ecdsa_mod.N - 1, ecdsa_mod.N - 2),
        (0x1234567890abcdef, 0xfedcba0987654321),
    ]
    for i, (r, s) in enumerate(test_cases):
        encoded = ecdsa_mod._der_encode_sig(r, s)
        decoded_r, decoded_s = ecdsa_mod._der_decode_sig(encoded)
        test(f"DER round-trip {i+1} r", decoded_r == r)
        test(f"DER round-trip {i+1} s", decoded_s == s)

    return True


def test_der_decode_rejects_malformed():
    """Test 8: DER decoder rejects malformed input."""
    print()
    print("=== Test 8: DER decoder rejects malformed input ===")
    import ecdsa as ecdsa_mod

    # Too short
    try:
        ecdsa_mod._der_decode_sig(b"\x30\x06\x02\x01")
        test("rejects too-short DER", False)
    except (ValueError, IndexError):
        test("rejects too-short DER", True)

    # Wrong SEQUENCE tag
    try:
        ecdsa_mod._der_decode_sig(b"\x31\x06\x02\x01\x01\x02\x01\x01")
        test("rejects wrong SEQUENCE tag", False)
    except ValueError:
        test("rejects wrong SEQUENCE tag", True)

    # Wrong INTEGER tag for r
    try:
        ecdsa_mod._der_decode_sig(b"\x30\x06\x03\x01\x01\x02\x01\x01")
        test("rejects wrong INTEGER tag", False)
    except ValueError:
        test("rejects wrong INTEGER tag", True)

    # r=0 (invalid)
    try:
        ecdsa_mod._der_decode_sig(b"\x30\x06\x02\x01\x00\x02\x01\x01")
        test("rejects r=0", False)
    except ValueError:
        test("rejects r=0", True)

    # s=0 (invalid)
    try:
        ecdsa_mod._der_decode_sig(b"\x30\x06\x02\x01\x01\x02\x01\x00")
        test("rejects s=0", False)
    except ValueError:
        test("rejects s=0", True)

    return True


def main():
    global passed, failed

    print("Heron ECDSA Verify Test")
    print("========================")
    print()
    print("Direction: cryptography sign -> pure-Python ecdsa.verify")
    print()

    priv, pub = load_update_keys()
    pub_bytes = pubkey_to_bytes(pub)
    print(f"Update key loaded: P-256")
    print(f"Public key: {len(pub_bytes)} bytes (uncompressed)")
    print()

    test_single_signature(priv, pub, pub_bytes)
    test_multiple_messages(priv, pub_bytes)
    test_tampered_signature(priv, pub_bytes)
    test_wrong_public_key(priv, pub_bytes)
    test_roundtrip(priv, pub_bytes)
    test_bundle_signature(priv, pub_bytes)
    test_der_roundtrip()
    test_der_decode_rejects_malformed()

    print()
    print("========================")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("ALL TESTS PASSED")
        print("ecdsa.py can verify signatures produced by the cryptography library.")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()

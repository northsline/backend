#!/usr/bin/env python3
"""
test_bundle.py. Round-trip test for the .heronpkg bundle format.

Requires: cryptography, a real firmware tree at ../firmware/.

This test:
  1. Generates a fresh update key pair if none exists.
  2. Builds a .heronpkg from the real firmware main.py + lib/*.py.
  3. Parses the bundle back.
  4. Verifies the ECDSA P-256 signature.
  5. Verifies every file's SHA-256 against the manifest.
  6. Confirms reconstructed files match the originals byte-for-byte.

Run with: python3 test_bundle.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "heron-cli"))

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend

import keygen
from heron_cli import bundle


def ensure_update_keys():
    """Generate update keys if they don't exist."""
    if not os.path.exists(keygen.UPDATE_PRIVATE_PATH):
        print("  No update key pair found; generating one...")
        keygen.init_update()
    else:
        print("  Reusing existing update key pair.")


def load_update_keys():
    with open(keygen.UPDATE_PRIVATE_PATH, "rb") as f:
        priv = serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )
    with open(keygen.UPDATE_PUBLIC_PATH, "rb") as f:
        pub = serialization.load_pem_public_key(
            f.read(), backend=default_backend()
        )
    return priv, pub


def load_firmware_files():
    """Read the real firmware tree from ../firmware/."""
    firmware_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware")
    files = {}
    main_path = os.path.join(firmware_dir, "main.py")
    if not os.path.exists(main_path):
        raise RuntimeError(f"No firmware main.py found at {main_path}")
    with open(main_path, "rb") as f:
        files["main.py"] = f.read()

    lib_dir = os.path.join(firmware_dir, "lib")
    if os.path.isdir(lib_dir):
        for name in sorted(os.listdir(lib_dir)):
            if not name.endswith(".py"):
                continue
            file_path = os.path.join(lib_dir, name)
            if not os.path.isfile(file_path):
                continue
            with open(file_path, "rb") as f:
                files[f"lib/{name}"] = f.read()

    return files


def main():
    print("Heron Bundle Round-Trip Test")
    print("============================")
    print()

    ensure_update_keys()
    priv, pub = load_update_keys()
    print(f"Update key: {keygen.UPDATE_PRIVATE_PATH}")
    print(f"Public key: {keygen.UPDATE_PUBLIC_PATH}")
    print()

    files = load_firmware_files()
    print(f"Loaded {len(files)} firmware files:")
    for path in sorted(files.keys()):
        print(f"  {path}: {len(files[path])} bytes")
    print()

    print("=== Test 1: Build bundle ===")
    pkg = bundle.build_bundle(
        files=files,
        fw_version=2,
        min_from=1,
        private_key=priv,
    )
    print(f"  Bundle size: {len(pkg)} bytes")

    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "dist",
        "heron-fw-2.heronpkg"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(pkg)
    print(f"  Wrote: {output_path}")
    print("  PASS: bundle built")
    print()

    print("=== Test 2: Parse and verify signature ===")
    parsed = bundle.parse_bundle(pkg, public_key=pub)
    print(f"  Firmware version: {parsed.fw_version}")
    print(f"  Min from: {parsed.min_from}")
    print(f"  Created at: {parsed.created_at}")
    print(f"  Files in manifest: {len(parsed.manifest['files'])}")
    print(f"  Signature: {len(parsed.raw_signature)} bytes")
    print("  PASS: parsed and signature verified")
    print()

    print("=== Test 3: Verify file hashes and contents ===")
    if not parsed.file_hashes_match():
        print("  FAIL: file hashes do not match manifest")
        sys.exit(1)

    for path in sorted(files.keys()):
        if files[path] != parsed.files.get(path, b""):
            print(f"  FAIL: reconstructed {path} does not match original")
            sys.exit(1)
    print("  PASS: all file hashes and contents match")
    print()

    print("=== Test 4: Invalid signature is rejected ===")
    bad_pkg = bytearray(pkg)
    # Flip a bit in the signature region after the header (offset 6 onwards).
    bad_pkg[10] ^= 0xFF
    try:
        bundle.parse_bundle(bytes(bad_pkg), public_key=pub)
        print("  FAIL: tampered bundle was accepted")
        sys.exit(1)
    except bundle.BundleError as e:
        print(f"  PASS: tampered bundle rejected ({e})")
    print()

    print("=== Test 5: bundle_from_directory convenience ===")
    tmpdir = tempfile.mkdtemp(prefix="heron_bundle_test_")
    dir_output = os.path.join(tmpdir, "from-dir.heronpkg")
    firmware_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware")
    bundle.bundle_from_directory(
        directory=firmware_dir,
        fw_version=3,
        min_from=1,
        private_key=priv,
        output_path=dir_output,
    )
    with open(dir_output, "rb") as f:
        dir_pkg = f.read()
    parsed_dir = bundle.parse_bundle(dir_pkg, public_key=pub)
    if not parsed_dir.file_hashes_match():
        print("  FAIL: directory bundle hashes do not match")
        sys.exit(1)
    print(f"  Wrote: {dir_output} ({len(dir_pkg)} bytes)")
    print("  PASS: directory bundle verified")
    print()

    print("============================")
    print("ALL TESTS PASSED")
    print("The .heronpkg format builds, signs, parses, and verifies end-to-end.")
    print()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
keygen.py: cryptographic key generation for Heron devices

Generates:
  1. The Northsline root key pair (once: keep the private key safe)
  2. Per-device key pairs + certificates (one per device)
  3. Outputs the root public key in JWK format for embedding in the PWA

Usage:
    python keygen.py --init-root          # Generate root key pair (run once)
    python keygen.py --device              # Generate a device key pair + cert
    python keygen.py --device --serial 01  # Use a specific serial number

Requires: pip install cryptography
"""

import argparse
import os
import sys
import json
import secrets
import base64

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend

# Key storage paths (relative to backend/)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_KEY_DIR = os.path.join(SCRIPT_DIR, "keys")
ROOT_PRIVATE_PATH = os.path.join(ROOT_KEY_DIR, "root_private.pem")
ROOT_PUBLIC_PATH = os.path.join(ROOT_KEY_DIR, "root_public.pem")
ROOT_JWK_PATH = os.path.join(ROOT_KEY_DIR, "root_public.jwk")
UPDATE_PRIVATE_PATH = os.path.join(ROOT_KEY_DIR, "update_private.pem")
UPDATE_PUBLIC_PATH = os.path.join(ROOT_KEY_DIR, "update_public.pem")
UPDATE_PUBLIC_BIN_PATH = os.path.join(ROOT_KEY_DIR, "update_public.bin")
UPDATE_PUBLIC_JWK_PATH = os.path.join(ROOT_KEY_DIR, "update_public.jwk")


def _b64url(data: bytes) -> str:
    """Base64url encode without padding (for JWK)."""
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += '=' * pad
    return base64.urlsafe_b64decode(s)


def _generate_p256_keypair():
    """Generate an ECDSA P-256 key pair and return (private_key, public_key)."""
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    public_key = private_key.public_key()
    return private_key, public_key


def _save_private_key(private_key, path):
    """Save a private key to PEM with restrictive permissions."""
    pem_priv = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    with open(path, 'wb') as f:
        f.write(pem_priv)
    os.chmod(path, 0o600)


def _save_public_key_pem(public_key, path):
    """Save a public key to PEM."""
    pem_pub = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with open(path, 'wb') as f:
        f.write(pem_pub)


def _public_key_to_jwk(public_key):
    """Return a JWK dict for an ECDSA P-256 public key."""
    nums = public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(nums.x.to_bytes(32, 'big')),
        "y": _b64url(nums.y.to_bytes(32, 'big')),
        "ext": True,
    }


def _public_key_uncompressed_bytes(public_key):
    """Return 65-byte uncompressed public key: 0x04 || X || Y."""
    nums = public_key.public_numbers()
    return b'\x04' + nums.x.to_bytes(32, 'big') + nums.y.to_bytes(32, 'big')


def init_root():
    """Generate the Northsline root ECDSA P-256 key pair.

    The private key signs all device certificates.
    The public key goes into the PWA (root-key.ts) for verification.
    """
    os.makedirs(ROOT_KEY_DIR, exist_ok=True)

    if os.path.exists(ROOT_PRIVATE_PATH):
        print(f"ERROR: Root private key already exists at {ROOT_PRIVATE_PATH}")
        print("Delete it first if you really want to regenerate (this invalidates all existing devices).")
        sys.exit(1)

    # Generate P-256 key pair
    private_key, public_key = _generate_p256_keypair()

    _save_private_key(private_key, ROOT_PRIVATE_PATH)
    _save_public_key_pem(public_key, ROOT_PUBLIC_PATH)

    # Export public key as JWK for the PWA
    jwk = _public_key_to_jwk(public_key)
    with open(ROOT_JWK_PATH, 'w') as f:
        json.dump(jwk, f, indent=2)

    print(f"Root key pair generated:")
    print(f"  Private: {ROOT_PRIVATE_PATH} (KEEP SECRET: protect with your life)")
    print(f"  Public:  {ROOT_PUBLIC_PATH}")
    print(f"  JWK:     {ROOT_JWK_PATH}")
    print()
    print("Paste this into onboard/src/lib/root-key.ts:")
    print()
    print(f"  x: '{jwk['x']}',")
    print(f"  y: '{jwk['y']}',")
    print()


def init_update():
    """Generate the Heron firmware-update ECDSA P-256 key pair.

    The private key signs firmware bundles (.heronpkg).
    The public key is burned into devices during manufacturing; devices and
    the CLI use it to verify bundles before applying them.

    This key is independent from the attestation root key so each can rotate
    without dragging the other.
    """
    os.makedirs(ROOT_KEY_DIR, exist_ok=True)

    if os.path.exists(UPDATE_PRIVATE_PATH):
        print(f"ERROR: Update private key already exists at {UPDATE_PRIVATE_PATH}")
        print("Delete it first if you really want to regenerate (this invalidates existing bundle signatures).")
        sys.exit(1)

    private_key, public_key = _generate_p256_keypair()

    _save_private_key(private_key, UPDATE_PRIVATE_PATH)
    _save_public_key_pem(public_key, UPDATE_PUBLIC_PATH)

    # 65-byte uncompressed point for burning into OTP/flash
    pub_bytes = _public_key_uncompressed_bytes(public_key)
    with open(UPDATE_PUBLIC_BIN_PATH, 'wb') as f:
        f.write(pub_bytes)

    # JWK for CLI / dashboard verification convenience
    jwk = _public_key_to_jwk(public_key)
    with open(UPDATE_PUBLIC_JWK_PATH, 'w') as f:
        json.dump(jwk, f, indent=2)

    print(f"Update key pair generated:")
    print(f"  Private: {UPDATE_PRIVATE_PATH} (KEEP SECRET: manufacturing machine only)")
    print(f"  Public:  {UPDATE_PUBLIC_PATH}")
    print(f"  Binary:  {UPDATE_PUBLIC_BIN_PATH} ({len(pub_bytes)} bytes, for device burn)")
    print(f"  JWK:     {UPDATE_PUBLIC_JWK_PATH}")
    print()


def load_root_private():
    """Load the root private key for signing device certificates."""
    if not os.path.exists(ROOT_PRIVATE_PATH):
        print(f"ERROR: Root private key not found at {ROOT_PRIVATE_PATH}")
        print("Run: python keygen.py --init-root")
        sys.exit(1)
    with open(ROOT_PRIVATE_PATH, 'rb') as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


def generate_device(serial_hex: str = None):
    """Generate a per-device key pair and certificate.

    Returns dict with:
        private_key: 32 bytes (raw)
        public_key: 65 bytes (uncompressed, 0x04 || X || Y)
        serial: 8 bytes
        certificate: bytes (DER-encoded, signed by root)
    """
    root_priv = load_root_private()

    # Generate device key pair
    dev_priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    dev_pub = dev_priv.public_key()

    # Extract raw private key (32 bytes)
    priv_nums = dev_priv.private_numbers()
    priv_bytes = priv_nums.private_value.to_bytes(32, 'big')

    # Extract uncompressed public key (65 bytes: 0x04 || X || Y)
    pub_nums = dev_pub.public_numbers()
    pub_bytes = b'\x04' + pub_nums.x.to_bytes(32, 'big') + pub_nums.y.to_bytes(32, 'big')

    # Serial number (8 random bytes, or user-specified)
    if serial_hex:
        serial_bytes = bytes.fromhex(serial_hex)
        if len(serial_bytes) != 8:
            print("ERROR: serial must be 8 bytes (16 hex chars)")
            sys.exit(1)
    else:
        serial_bytes = secrets.token_bytes(8)

    # Build certificate: serial || pubKey, signed by root
    cert_message = serial_bytes + pub_bytes
    signature = root_priv.sign(cert_message, ec.ECDSA(hashes.SHA256()))

    # Build the binary certificate format (matches what otp_keys.py reads):
    # [1 byte: sig_len] [sig_len bytes: DER sig] [8 bytes: serial] [65 bytes: pubKey]
    cert_binary = bytes([len(signature)]) + signature + serial_bytes + pub_bytes

    return {
        'private_key': priv_bytes,
        'public_key': pub_bytes,
        'serial': serial_bytes,
        'serial_hex': serial_bytes.hex(),
        'certificate': cert_binary,
    }


def main():
    parser = argparse.ArgumentParser(description="Heron cryptographic key generation")
    parser.add_argument('--init-root', action='store_true',
                        help='Generate the Northsline root key pair (run once)')
    parser.add_argument('--init-update', action='store_true',
                        help='Generate the firmware update key pair (run once)')
    parser.add_argument('--device', action='store_true',
                        help='Generate a per-device key pair + certificate')
    parser.add_argument('--serial', type=str, default=None,
                        help='Device serial number (16 hex chars). Random if omitted.')
    args = parser.parse_args()

    if args.init_root:
        init_root()
    elif args.init_update:
        init_update()
    elif args.device:
        dev = generate_device(args.serial)
        print(f"Device serial: {dev['serial_hex']}")
        print(f"Private key:   {dev['private_key'].hex()}")
        print(f"Public key:    {dev['public_key'].hex()}")
        print(f"Certificate:   {dev['certificate'].hex()}")
        print(f"Cert length:   {len(dev['certificate'])} bytes")
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
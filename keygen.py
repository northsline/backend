#!/usr/bin/env python3
"""
keygen.py — cryptographic key generation for Known devices

Generates:
  1. The Northsline root key pair (once — keep the private key safe)
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


def _b64url(data: bytes) -> str:
    """Base64url encode without padding (for JWK)."""
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += '=' * pad
    return base64.urlsafe_b64decode(s)


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
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    public_key = private_key.public_key()

    # Save private key (PEM, no password — protect with file permissions)
    pem_priv = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    with open(ROOT_PRIVATE_PATH, 'wb') as f:
        f.write(pem_priv)
    os.chmod(ROOT_PRIVATE_PATH, 0o600)

    # Save public key (PEM)
    pem_pub = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with open(ROOT_PUBLIC_PATH, 'wb') as f:
        f.write(pem_pub)

    # Export public key as JWK for the PWA
    nums = public_key.public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(nums.x.to_bytes(32, 'big')),
        "y": _b64url(nums.y.to_bytes(32, 'big')),
        "ext": True,
    }
    with open(ROOT_JWK_PATH, 'w') as f:
        json.dump(jwk, f, indent=2)

    print(f"Root key pair generated:")
    print(f"  Private: {ROOT_PRIVATE_PATH} (KEEP SECRET — protect with your life)")
    print(f"  Public:  {ROOT_PUBLIC_PATH}")
    print(f"  JWK:     {ROOT_JWK_PATH}")
    print()
    print("Paste this into onboard/src/lib/root-key.ts:")
    print()
    print(f"  x: '{jwk['x']}',")
    print(f"  y: '{jwk['y']}',")
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
    parser = argparse.ArgumentParser(description="Known cryptographic key generation")
    parser.add_argument('--init-root', action='store_true',
                        help='Generate the Northsline root key pair (run once)')
    parser.add_argument('--device', action='store_true',
                        help='Generate a per-device key pair + certificate')
    parser.add_argument('--serial', type=str, default=None,
                        help='Device serial number (16 hex chars). Random if omitted.')
    args = parser.parse_args()

    if args.init_root:
        init_root()
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
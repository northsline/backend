"""
bundle.py: build and parse Heron firmware bundles (.heronpkg).

Bundle format:
    [ magic    4B ]  b"HRNP"
    [ ver      1B ]  bundle format version (0x01)
    [ sigLen   1B ]  length of DER signature (70-72 typically)
    [ sig    sigLenB ]  ECDSA P-256 SHA-256 signature over SHA-256(manifest || files)
    [ mLen    2B ]  little-endian manifest length
    [ manifest mLenB ]  JSON
    [ files ...    ]  concatenated file blobs, order matches manifest

The signature covers SHA-256(manifest_bytes || concatenated_file_blobs).
Only main.py and lib/**.py are valid bundle targets.
"""

import hashlib
import json
import os
import time
from typing import Dict, List, Optional

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidSignature

MAGIC = b"HRNP"
FORMAT_VERSION = 0x01
MAX_SIG_LEN = 255
MAX_MANIFEST_LEN = 0xFFFF
VALID_PATH_PREFIXES = ("main.py", "lib/")


class BundleError(Exception):
    """Raised when a bundle is malformed or fails verification."""
    pass


class Bundle:
    """A parsed Heron firmware bundle."""

    def __init__(self, manifest: dict, files: Dict[str, bytes], raw_signature: bytes):
        self.manifest = manifest
        self.files = files
        self.raw_signature = raw_signature

    @property
    def fw_version(self) -> int:
        return self.manifest["fw_version"]

    @property
    def min_from(self) -> int:
        return self.manifest["min_from"]

    @property
    def created_at(self) -> int:
        return self.manifest["created_at"]

    def file_hashes_match(self) -> bool:
        """Verify each file's SHA-256 against the manifest."""
        for entry in self.manifest["files"]:
            path = entry["path"]
            expected = entry["sha256"]
            actual = hashlib.sha256(self.files.get(path, b"")).hexdigest()
            if actual != expected:
                return False
        return True


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_paths(paths: List[str]):
    for path in paths:
        if path == "main.py":
            continue
        if path.startswith("lib/") and len(path) > len("lib/"):
            continue
        raise BundleError(
            f"Invalid bundle path: {path!r}. Only main.py and lib/**.py are allowed."
        )


def _build_manifest(files: Dict[str, bytes], fw_version: int, min_from: int) -> dict:
    file_list = []
    for path in sorted(files.keys()):
        data = files[path]
        if not isinstance(data, (bytes, bytearray)):
            raise BundleError(f"File {path!r} data must be bytes")
        file_list.append({
            "path": path,
            "size": len(data),
            "sha256": _sha256_hex(data),
        })

    return {
        "fw_version": int(fw_version),
        "min_from": int(min_from),
        "created_at": int(time.time()),
        "files": file_list,
    }


def _pack_le16(value: int) -> bytes:
    return value.to_bytes(2, "little")


def _unpack_le16(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise BundleError("Truncated bundle: expected 2-byte length")
    return int.from_bytes(data[offset:offset + 2], "little")


def _load_pem_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )


def _load_pem_public_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_public_key(
            f.read(), backend=default_backend()
        )


def _sign(private_key, data: bytes) -> bytes:
    return private_key.sign(data, ec.ECDSA(hashes.SHA256()))


def _verify(public_key, signature: bytes, data: bytes) -> bool:
    try:
        public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


def _files_to_blob(files: Dict[str, bytes], manifest: dict) -> bytes:
    """Concatenate file blobs in manifest order."""
    blobs = []
    for entry in manifest["files"]:
        path = entry["path"]
        if path not in files:
            raise BundleError(f"Manifest references missing file: {path!r}")
        blobs.append(files[path])
    return b"".join(blobs)


def build_bundle(files: Dict[str, bytes], fw_version: int, min_from: int,
                 private_key) -> bytes:
    """Build a signed .heronpkg from a dict of relative paths to file bytes.

    Args:
        files: mapping of relative paths (e.g. "main.py", "lib/dns_monitor.py")
               to file contents.
        fw_version: monotonic firmware version number.
        min_from: minimum device firmware version that can apply this bundle.
        private_key: ECDSA P-256 private key object from cryptography, or a PEM
                     file path string.

    Returns:
        Raw bytes of the signed bundle.
    """
    if isinstance(private_key, str):
        private_key = _load_pem_private_key(private_key)

    _validate_paths(list(files.keys()))
    manifest = _build_manifest(files, fw_version, min_from)
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    if len(manifest_bytes) > MAX_MANIFEST_LEN:
        raise BundleError(f"Manifest too large: {len(manifest_bytes)} bytes (max {MAX_MANIFEST_LEN})")

    files_blob = _files_to_blob(files, manifest)
    signed_digest = _sha256(manifest_bytes + files_blob)
    signature = _sign(private_key, signed_digest)

    if len(signature) > MAX_SIG_LEN:
        raise BundleError(f"Signature too long: {len(signature)} bytes (max {MAX_SIG_LEN})")

    return (
        MAGIC
        + bytes([FORMAT_VERSION])
        + bytes([len(signature)])
        + signature
        + _pack_le16(len(manifest_bytes))
        + manifest_bytes
        + files_blob
    )


def parse_bundle(data: bytes, public_key=None) -> Bundle:
    """Parse a bundle and optionally verify its signature.

    Args:
        data: raw bundle bytes.
        public_key: optional ECDSA P-256 public key object or PEM file path.
                    If provided, the signature is verified against
                    SHA-256(manifest || files).

    Returns:
        A Bundle object.
    """
    if public_key is not None and isinstance(public_key, str):
        public_key = _load_pem_public_key(public_key)

    offset = 0

    # magic
    if len(data) < 4 or data[:4] != MAGIC:
        raise BundleError("Invalid bundle: bad magic")
    offset += 4

    # version
    if len(data) < offset + 1:
        raise BundleError("Invalid bundle: truncated version byte")
    version = data[offset]
    if version != FORMAT_VERSION:
        raise BundleError(f"Unsupported bundle version: {version}")
    offset += 1

    # signature length
    if len(data) < offset + 1:
        raise BundleError("Invalid bundle: truncated signature length")
    sig_len = data[offset]
    offset += 1

    # signature
    if len(data) < offset + sig_len:
        raise BundleError("Invalid bundle: truncated signature")
    signature = data[offset:offset + sig_len]
    offset += sig_len

    # manifest length
    manifest_len = _unpack_le16(data, offset)
    offset += 2

    # manifest
    if len(data) < offset + manifest_len:
        raise BundleError("Invalid bundle: truncated manifest")
    manifest_bytes = data[offset:offset + manifest_len]
    offset += manifest_len

    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BundleError(f"Invalid bundle: manifest is not valid JSON: {e}")

    required = {"fw_version", "min_from", "created_at", "files"}
    missing = required - set(manifest.keys())
    if missing:
        raise BundleError(f"Invalid bundle: manifest missing keys {missing}")

    # file blobs
    files: Dict[str, bytes] = {}
    for entry in manifest["files"]:
        path = entry["path"]
        size = entry["size"]
        if len(data) < offset + size:
            raise BundleError(f"Invalid bundle: truncated file data for {path!r}")
        files[path] = bytes(data[offset:offset + size])
        offset += size

    if offset != len(data):
        raise BundleError(f"Invalid bundle: {len(data) - offset} trailing bytes after file data")

    if public_key is not None:
        files_blob = _files_to_blob(files, manifest)
        signed_digest = _sha256(manifest_bytes + files_blob)
        if not _verify(public_key, signature, signed_digest):
            raise BundleError("Invalid bundle: signature verification failed")

    return Bundle(manifest, files, signature)


def bundle_from_directory(directory: str, fw_version: int, min_from: int,
                          private_key, output_path: str) -> None:
    """Convenience: read main.py + lib/*.py from a directory and write a bundle."""
    files: Dict[str, bytes] = {}
    main_path = os.path.join(directory, "main.py")
    if not os.path.exists(main_path):
        raise BundleError(f"No main.py found in {directory}")
    with open(main_path, "rb") as f:
        files["main.py"] = f.read()

    lib_dir = os.path.join(directory, "lib")
    if os.path.isdir(lib_dir):
        for name in sorted(os.listdir(lib_dir)):
            if not name.endswith(".py"):
                continue
            file_path = os.path.join(lib_dir, name)
            if not os.path.isfile(file_path):
                continue
            with open(file_path, "rb") as f:
                files[f"lib/{name}"] = f.read()

    bundle = build_bundle(files, fw_version, min_from, private_key)
    with open(output_path, "wb") as f:
        f.write(bundle)


def verify_bundle(data: bytes, public_key) -> bool:
    """Quick verification helper: parse and verify signature + file hashes."""
    if isinstance(public_key, str):
        public_key = _load_pem_public_key(public_key)
    bundle = parse_bundle(data, public_key)
    return bundle.file_hashes_match()

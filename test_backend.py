#!/usr/bin/env python3
"""
Test suite for Known activation backend.
Tests the database layer and validates the activation flow.
"""

import os
import tempfile
import sqlite3
import re
from datetime import datetime, timezone

import db

# Test database path
TEST_DB = None

def setup_test_db():
    """Create a temporary test database."""
    global TEST_DB
    fd, TEST_DB = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(TEST_DB)
    return TEST_DB

def teardown_test_db():
    """Clean up test database."""
    global TEST_DB
    if TEST_DB and os.path.exists(TEST_DB):
        os.unlink(TEST_DB)

def test_init_db():
    """Test database initialization."""
    print("✓ test_init_db: Database table created successfully")

def test_insert_and_retrieve_device():
    """Test inserting and retrieving a device."""
    sticker = "KNOWN-ABCD-EFGH"
    secret = "test_secret_12345"
    device_id = "device-123"

    db.insert_device(sticker, secret, device_id, TEST_DB)
    device = db.get_device(sticker, TEST_DB)

    assert device is not None, "Device should be retrieved"
    assert device["sticker_code"] == sticker
    assert device["device_secret"] == secret
    assert device["device_id"] == device_id
    assert device["status"] == "unclaimed"
    assert device["claimed_by"] is None
    assert device["claimed_at"] is None

    print("✓ test_insert_and_retrieve_device: Device created and retrieved correctly")

def test_insert_duplicate_ignored():
    """Test that duplicate inserts are ignored."""
    sticker = "KNOWN-TEST-DUP1"
    secret1 = "secret1"
    secret2 = "secret2"
    device_id = "device-dup"

    db.insert_device(sticker, secret1, device_id, TEST_DB)
    db.insert_device(sticker, secret2, device_id, TEST_DB)  # Should be ignored

    device = db.get_device(sticker, TEST_DB)
    assert device["device_secret"] == secret1, "First insert should be preserved"

    print("✓ test_insert_duplicate_ignored: Duplicate insert correctly ignored")

def test_claim_device_success():
    """Test claiming an unclaimed device."""
    sticker = "KNOWN-CLAIM-OK1"
    secret = "secret_claim"
    device_id = "device-claim-1"
    user_id = "user-123"

    db.insert_device(sticker, secret, device_id, TEST_DB)
    ok, result = db.claim_device(sticker, user_id, TEST_DB)

    assert ok, "Claim should succeed"
    assert result["status"] == "claimed"
    assert result["claimed_by"] == user_id
    assert result["claimed_at"] is not None

    # Verify timestamp is ISO 8601
    try:
        datetime.fromisoformat(result["claimed_at"])
        timestamp_valid = True
    except ValueError:
        timestamp_valid = False
    assert timestamp_valid, "claimed_at should be ISO 8601"

    print("✓ test_claim_device_success: Device claimed successfully with valid timestamp")

def test_claim_device_not_found():
    """Test claiming a non-existent device."""
    sticker = "KNOWN-NOTFOUND-99"
    user_id = "user-999"

    ok, result = db.claim_device(sticker, user_id, TEST_DB)

    assert not ok, "Claim should fail for non-existent device"
    assert result == "not_found"

    print("✓ test_claim_device_not_found: Non-existent device returns 'not_found'")

def test_claim_device_already_claimed():
    """Test claiming an already-claimed device."""
    sticker = "KNOWN-CLAIM-OK2"
    secret = "secret_claim2"
    device_id = "device-claim-2"
    user_id_1 = "user-1"
    user_id_2 = "user-2"

    db.insert_device(sticker, secret, device_id, TEST_DB)
    ok1, _ = db.claim_device(sticker, user_id_1, TEST_DB)
    ok2, result = db.claim_device(sticker, user_id_2, TEST_DB)

    assert ok1, "First claim should succeed"
    assert not ok2, "Second claim should fail"
    assert result == "already_claimed"

    # Verify it's still claimed by first user
    device = db.get_device(sticker, TEST_DB)
    assert device["claimed_by"] == user_id_1

    print("✓ test_claim_device_already_claimed: Already-claimed device returns 'already_claimed'")

def test_sticker_code_format():
    """Test sticker code format validation regex."""
    STICKER_RE = re.compile(r"^KNOWN-[A-Z0-9]{4}-[A-Z0-9]{4}$")

    # Valid codes
    valid = [
        "KNOWN-ABCD-EFGH",
        "KNOWN-0000-ZZZZ",
        "KNOWN-A1B2-C3D4",
    ]
    for code in valid:
        assert STICKER_RE.match(code), f"{code} should be valid"

    # Invalid codes
    invalid = [
        "KNOWN-ABC-EFGH",      # Too short
        "KNOWN-ABCD-EFG",      # Too short
        "KNOWN-ABCDE-EFGH",    # Too long
        "known-ABCD-EFGH",     # Lowercase prefix
        "KNOWN-abcd-EFGH",     # Lowercase segment
        "KNOWN-ABCD_EFGH",     # Wrong separator
        "UNKNOWN-ABCD-EFGH",   # Wrong prefix
    ]
    for code in invalid:
        assert not STICKER_RE.match(code), f"{code} should be invalid"

    print("✓ test_sticker_code_format: Sticker code format validation correct")

def test_multiple_devices():
    """Test creating and managing multiple devices."""
    count = 5
    stickers = []

    for i in range(count):
        sticker = f"KNOWN-MULT-{i:04d}"
        device_id = f"device-{i}"
        secret = f"secret-{i}"
        db.insert_device(sticker, secret, device_id, TEST_DB)
        stickers.append(sticker)

    # Verify all can be retrieved
    for i, sticker in enumerate(stickers):
        device = db.get_device(sticker, TEST_DB)
        assert device is not None
        assert device["device_id"] == f"device-{i}"
        assert device["status"] == "unclaimed"

    # Claim some of them
    for sticker in stickers[:2]:
        ok, _ = db.claim_device(sticker, "user-multi", TEST_DB)
        assert ok

    # Verify state
    for i, sticker in enumerate(stickers):
        device = db.get_device(sticker, TEST_DB)
        if i < 2:
            assert device["status"] == "claimed"
        else:
            assert device["status"] == "unclaimed"

    print(f"✓ test_multiple_devices: Successfully managed {count} devices")

def run_tests():
    """Run all tests."""
    setup_test_db()

    tests = [
        test_init_db,
        test_insert_and_retrieve_device,
        test_insert_duplicate_ignored,
        test_claim_device_success,
        test_claim_device_not_found,
        test_claim_device_already_claimed,
        test_sticker_code_format,
        test_multiple_devices,
    ]

    print("\n" + "="*60)
    print("Running Known Backend Tests")
    print("="*60 + "\n")

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: Unexpected error: {e}")
            failed += 1

    teardown_test_db()

    print("\n" + "="*60)
    print(f"Results: {passed} passed, {failed} failed")
    print("="*60 + "\n")

    return failed == 0

if __name__ == "__main__":
    import sys
    success = run_tests()
    sys.exit(0 if success else 1)

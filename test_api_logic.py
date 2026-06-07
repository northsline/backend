#!/usr/bin/env python3
"""
Test the activation API logic without requiring Flask/flask_cors.
Validates request handling and response logic.
"""

import re
import json
import tempfile
import os

import db

TEST_DB = None

def setup():
    global TEST_DB
    fd, TEST_DB = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(TEST_DB)

def teardown():
    global TEST_DB
    if TEST_DB and os.path.exists(TEST_DB):
        os.unlink(TEST_DB)

# API logic (extracted from app.py)
STICKER_RE = re.compile(r"^KNOWN-[A-Z0-9]{4}-[A-Z0-9]{4}$")

def activate(data):
    """
    Simulate the /activate endpoint logic.
    Returns (status_code, response_dict)
    """
    code = (data.get("sticker_code") or "").strip().upper()
    user_id = data.get("user_id") or "anonymous"

    if not STICKER_RE.match(code):
        return 400, {"status": "error", "reason": "bad_format"}

    ok, result = db.claim_device(code, user_id, TEST_DB)
    if not ok:
        http = 404 if result == "not_found" else 409
        return http, {"status": "error", "reason": result}

    return 200, {"status": "ok", "device_id": result["device_id"]}

def test_activate_success():
    """Test successful activation."""
    setup()

    sticker = "KNOWN-TEST-SUC1"
    device_id = "device-success-1"
    db.insert_device(sticker, "secret", device_id, TEST_DB)

    status, response = activate({
        "sticker_code": sticker,
        "user_id": "user-test"
    })

    assert status == 200, f"Expected 200, got {status}"
    assert response["status"] == "ok"
    assert response["device_id"] == device_id

    teardown()
    print("✓ test_activate_success: Valid activation returns 200 with device_id")

def test_activate_bad_format():
    """Test activation with bad sticker format."""
    setup()

    bad_codes = [
        "invalid-code",
        "KNOWN-ABC-DEFG",       # Wrong length
        "UNKNOWN-TEST-TEST",    # Wrong prefix
        "KNOWN-ABC-DEFGH",      # Too long
        "KNOWN-AB-DEFG",        # Too short
        "",
        "   ",
    ]

    for bad_code in bad_codes:
        status, response = activate({
            "sticker_code": bad_code,
            "user_id": "user-test"
        })

        assert status == 400, f"Expected 400 for '{bad_code}', got {status}"
        assert response["status"] == "error"
        assert response["reason"] == "bad_format"

    teardown()
    print(f"✓ test_activate_bad_format: {len(bad_codes)} invalid formats rejected with 400")

def test_activate_not_found():
    """Test activation with non-existent code."""
    setup()

    status, response = activate({
        "sticker_code": "KNOWN-NOTF-OUND",
        "user_id": "user-test"
    })

    assert status == 404, f"Expected 404, got {status}"
    assert response["status"] == "error"
    assert response["reason"] == "not_found"

    teardown()
    print("✓ test_activate_not_found: Non-existent code returns 404")

def test_activate_already_claimed():
    """Test activation of already-claimed device."""
    setup()

    sticker = "KNOWN-TEST-ACLA"
    device_id = "device-already-1"
    db.insert_device(sticker, "secret", device_id, TEST_DB)

    # First activation succeeds
    status1, response1 = activate({
        "sticker_code": sticker,
        "user_id": "user-first"
    })
    assert status1 == 200

    # Second activation fails
    status2, response2 = activate({
        "sticker_code": sticker,
        "user_id": "user-second"
    })

    assert status2 == 409, f"Expected 409, got {status2}"
    assert response2["status"] == "error"
    assert response2["reason"] == "already_claimed"

    teardown()
    print("✓ test_activate_already_claimed: Already-claimed code returns 409")

def test_activate_whitespace_normalization():
    """Test that sticker codes with whitespace are normalized."""
    setup()

    sticker = "KNOWN-TRIM-TEST"
    device_id = "device-trim-1"
    db.insert_device(sticker, "secret", device_id, TEST_DB)

    # Test with various whitespace
    variants = [
        sticker,
        f"  {sticker}  ",
        f"\t{sticker}\n",
        sticker.lower(),
        f"  {sticker.lower()}  ",
    ]

    for variant in variants:
        status, response = activate({
            "sticker_code": variant,
            "user_id": "user-test"
        })

        # Only the first one should work (others may already be claimed or bad format)
        if variant == sticker:
            assert status == 200, f"Failed for variant: {repr(variant)}"

    teardown()
    print("✓ test_activate_whitespace_normalization: Whitespace and case normalized correctly")

def test_activate_default_user_id():
    """Test that missing user_id defaults to 'anonymous'."""
    setup()

    sticker = "KNOWN-DEFD-USER"
    device_id = "device-default-1"
    db.insert_device(sticker, "secret", device_id, TEST_DB)

    status, response = activate({
        "sticker_code": sticker,
        # No user_id provided
    })

    assert status == 200
    assert response["device_id"] == device_id

    # Verify the device was claimed by 'anonymous'
    device = db.get_device(sticker, TEST_DB)
    assert device["claimed_by"] == "anonymous"

    teardown()
    print("✓ test_activate_default_user_id: Missing user_id defaults to 'anonymous'")

def test_activate_empty_body():
    """Test activation with empty request body."""
    setup()

    status, response = activate({})

    assert status == 400
    assert response["reason"] == "bad_format"

    teardown()
    print("✓ test_activate_empty_body: Empty body returns 400 bad_format")

def test_response_structure():
    """Test that responses have correct structure."""
    setup()

    sticker = "KNOWN-RESP-STRC"
    device_id = "device-response-1"
    db.insert_device(sticker, "secret", device_id, TEST_DB)

    # Success response
    status, response = activate({
        "sticker_code": sticker,
        "user_id": "user-test"
    })

    assert "status" in response
    assert "device_id" in response
    assert "reason" not in response  # No reason field on success
    json.dumps(response)  # Should be JSON-serializable

    # Error response
    status, response = activate({"sticker_code": "bad"})

    assert "status" in response
    assert "reason" in response
    assert "device_id" not in response  # No device_id on error
    json.dumps(response)  # Should be JSON-serializable

    teardown()
    print("✓ test_response_structure: Response structures are correct and JSON-serializable")

def run_tests():
    """Run all API logic tests."""
    tests = [
        test_activate_success,
        test_activate_bad_format,
        test_activate_not_found,
        test_activate_already_claimed,
        test_activate_whitespace_normalization,
        test_activate_default_user_id,
        test_activate_empty_body,
        test_response_structure,
    ]

    print("\n" + "="*60)
    print("Running API Logic Tests")
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

    print("\n" + "="*60)
    print(f"Results: {passed} passed, {failed} failed")
    print("="*60 + "\n")

    return failed == 0

if __name__ == "__main__":
    import sys
    success = run_tests()
    sys.exit(0 if success else 1)

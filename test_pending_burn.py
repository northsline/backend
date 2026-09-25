import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flash_known


def make_keys():
    serial = bytes.fromhex("1122334455667788")
    return {
        "private_key": b"\x11" * 32,
        "public_key": b"\x04" + b"\x22" * 64,
        "serial": serial,
        "serial_hex": serial.hex(),
        "certificate": b"\x30" + b"\x33" * 80,
    }


def test_pending_key_record_is_chip_bound_private_and_round_trips(tmp_path, monkeypatch):
    chip_id = "a1b2c3d4e5f60708"
    monkeypatch.setattr(flash_known, "PENDING_KEYS_DIR", str(tmp_path / "pending"))
    path = flash_known._pending_key_path(chip_id)
    keys = make_keys()

    flash_known._save_pending_keys(path, chip_id, keys)

    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / "pending").st_mode & 0o777 == 0o700
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    assert record["chip_id"] == chip_id
    assert flash_known._decode_pending_keys(record, chip_id) == keys


def test_pending_record_for_another_chip_is_rejected():
    record = {"chip_id": "a1b2c3d4", "private_key": "00"}
    try:
        flash_known._decode_pending_keys(record, "deadbeef")
    except ValueError as e:
        assert "different Pico" in str(e)
    else:
        assert False, "must not reuse one chip's keys on another Pico"


def test_existing_pending_record_is_reused_without_generating(tmp_path, monkeypatch):
    chip_id = "a1b2c3d4e5f60708"
    keys = make_keys()
    monkeypatch.setattr(flash_known, "PENDING_KEYS_DIR", str(tmp_path))
    path = flash_known._pending_key_path(chip_id)
    flash_known._save_pending_keys(path, chip_id, keys)
    monkeypatch.setattr(flash_known, "read_device_chip_id", lambda port: chip_id)

    # A valid pending record bypasses OTP reads and key generation.
    monkeypatch.setattr(flash_known, "_device_otp_state", lambda port: (_ for _ in ()).throw(AssertionError()))
    loaded, loaded_path = flash_known.get_or_create_pending_keys("/dev/mock")
    assert loaded == keys
    assert loaded_path == path


def test_fresh_otp_with_partial_data_is_not_given_a_new_identity(monkeypatch, tmp_path):
    chip_id = "a1b2c3d4e5f60708"
    monkeypatch.setattr(flash_known, "PENDING_KEYS_DIR", str(tmp_path))
    monkeypatch.setattr(flash_known, "read_device_chip_id", lambda port: chip_id)
    monkeypatch.setattr(flash_known, "_device_otp_state", lambda port: {
        "backend": "otp", "has_keys": False, "partial": True,
    })

    module = types.ModuleType("keygen")
    module.generate_device = lambda: (_ for _ in ()).throw(AssertionError("must not generate replacement keys"))
    monkeypatch.setitem(sys.modules, "keygen", module)

    keys, path = flash_known.get_or_create_pending_keys("/dev/mock")
    assert keys is None and path is None
    assert not os.path.exists(flash_known._pending_key_path(chip_id))


def test_fresh_blank_otp_persists_generated_keys_before_return(monkeypatch, tmp_path):
    chip_id = "a1b2c3d4e5f60708"
    keys = make_keys()
    monkeypatch.setattr(flash_known, "PENDING_KEYS_DIR", str(tmp_path))
    monkeypatch.setattr(flash_known, "read_device_chip_id", lambda port: chip_id)
    monkeypatch.setattr(flash_known, "_device_otp_state", lambda port: {
        "backend": "otp", "has_keys": False, "partial": False,
    })
    module = types.ModuleType("keygen")
    module.generate_device = lambda: keys
    monkeypatch.setitem(sys.modules, "keygen", module)

    generated, path = flash_known.get_or_create_pending_keys("/dev/mock")
    assert generated == keys
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    assert flash_known._decode_pending_keys(record, chip_id) == keys

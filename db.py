"""
SQLite device registry for Known activation.

One row per manufactured device. A sticker code is single-use: once /activate
claims it, `status` flips to 'claimed' and it is bound to a user permanently.

Schema:
    sticker_code   TEXT PRIMARY KEY   KNOWN-XXXX-XXXX
    device_secret  TEXT NOT NULL      per-device secret injected at manufacturing
    device_id      TEXT NOT NULL      stable device UUID
    status         TEXT NOT NULL      'unclaimed' | 'claimed'
    claimed_by     TEXT               user id, NULL until claimed
    created_at     TEXT NOT NULL      ISO 8601
    claimed_at     TEXT               ISO 8601, NULL until claimed
"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "codes.db"


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_conn(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=DB_PATH):
    """Create the registry table if it does not exist."""
    with get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                sticker_code  TEXT PRIMARY KEY,
                device_secret TEXT NOT NULL,
                device_id     TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'unclaimed',
                claimed_by    TEXT,
                created_at    TEXT NOT NULL,
                claimed_at    TEXT
            )
            """
        )
        conn.commit()


def insert_device(sticker_code, device_secret, device_id, db_path=DB_PATH):
    """Register a freshly manufactured device. Ignores duplicates."""
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO devices
                (sticker_code, device_secret, device_id, status, created_at)
            VALUES (?, ?, ?, 'unclaimed', ?)
            """,
            (sticker_code, device_secret, device_id, _now()),
        )
        conn.commit()


def get_device(sticker_code, db_path=DB_PATH):
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE sticker_code = ?", (sticker_code,)
        ).fetchone()
        return dict(row) if row else None


def claim_device(sticker_code, user_id, db_path=DB_PATH):
    """
    Claim an unclaimed code. Returns (ok, result):
      (True, device_dict)        on success
      (False, 'not_found')       unknown code
      (False, 'already_claimed') previously claimed
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE sticker_code = ?", (sticker_code,)
        ).fetchone()
        if row is None:
            return False, "not_found"
        if row["status"] == "claimed":
            return False, "already_claimed"
        conn.execute(
            """
            UPDATE devices
               SET status = 'claimed', claimed_by = ?, claimed_at = ?
             WHERE sticker_code = ?
            """,
            (user_id, _now(), sticker_code),
        )
        conn.commit()
        return True, get_device(sticker_code, db_path)

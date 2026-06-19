"""
SQLite device registry for Known activation.

One row per manufactured device. A sticker code is single-use: once /activate
claims it, `status` flips to 'claimed' and it is bound to a user permanently.

Schema:
    sticker_code      TEXT PRIMARY KEY   KNOWN-XXXX-XXXX
    device_secret     TEXT NOT NULL      per-device secret injected at manufacturing
    device_id         TEXT NOT NULL      stable device UUID
    status            TEXT NOT NULL      'unclaimed' | 'claimed'
    claimed_by        TEXT               user id, NULL until claimed
    created_at        TEXT NOT NULL      ISO 8601
    claimed_at        TEXT               ISO 8601, NULL until claimed
    site_id           TEXT               location/site label, NULL for consumer devices
    organization_id   TEXT               org/group label, NULL for consumer devices
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
                sticker_code   TEXT PRIMARY KEY,
                device_secret  TEXT NOT NULL,
                device_id      TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'unclaimed',
                claimed_by     TEXT,
                created_at     TEXT NOT NULL,
                claimed_at     TEXT,
                site_id        TEXT,
                organization_id TEXT
            )
            """
        )
        # Migrate existing tables: add columns if missing (safe on fresh tables).
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN site_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN organization_id TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def insert_device(sticker_code, device_secret, device_id, db_path=DB_PATH,
                  site_id=None, organization_id=None):
    """Register a freshly manufactured device. Ignores duplicates."""
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO devices
                (sticker_code, device_secret, device_id, status, created_at,
                 site_id, organization_id)
            VALUES (?, ?, ?, 'unclaimed', ?, ?, ?)
            """,
            (sticker_code, device_secret, device_id, _now(),
             site_id, organization_id),
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


def count_devices(db_path=DB_PATH):
    """Return (total, claimed) — cheap counts for /stats."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='claimed' THEN 1 ELSE 0 END) AS claimed "
            "FROM devices"
        ).fetchone()
    # SQLite returns None for SUM over zero rows; coerce to int.
    total = int(row["total"] or 0)
    claimed = int(row["claimed"] or 0)
    return total, claimed


def claimed_per_day(db_path=DB_PATH):
    """
    Group claimed devices by calendar day (UTC).
    Returns a list of {"date": "YYYY-MM-DD", "claimed": N} newest-first.
    Only days with at least one claim are returned.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT substr(claimed_at, 1, 10) AS day, COUNT(*) AS n "
            "  FROM devices "
            " WHERE status = 'claimed' AND claimed_at IS NOT NULL "
            " GROUP BY day "
            " ORDER BY day DESC"
        ).fetchall()
    return [{"date": r["day"], "claimed": int(r["n"])} for r in rows]


def devices_by_organization(organization_id, db_path=DB_PATH):
    """Return all devices belonging to an organization. For future MSP use."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM devices WHERE organization_id = ? ORDER BY created_at DESC",
            (organization_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_device_metadata(sticker_code, site_id=None, organization_id=None,
                        db_path=DB_PATH):
    """Stamp site/org metadata onto a device after activation.
    Called from /activate when site_id or organization_id is provided."""
    with get_conn(db_path) as conn:
        if site_id is not None:
            conn.execute(
                "UPDATE devices SET site_id = ? WHERE sticker_code = ?",
                (site_id, sticker_code)
            )
        if organization_id is not None:
            conn.execute(
                "UPDATE devices SET organization_id = ? WHERE sticker_code = ?",
                (organization_id, sticker_code)
            )
        conn.commit()


def claims_by_ip_today(ip, db_path=DB_PATH):
    """Count how many devices a single IP has claimed in the last 24h.
    Used to cap per-IP activation to prevent bulk code scraping."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM devices "
            "WHERE claimed_by = ? AND claimed_at >= ?",
            (ip, cutoff)
        ).fetchone()
    return int(row["n"] or 0)
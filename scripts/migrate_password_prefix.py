"""One-time migration: re-prefix legacy raw PBKDF2 password hashes.

Before the Argon2id migration, `users.password_hash` stored a raw 64-byte
salt+hash blob straight from `hashlib.pbkdf2_hmac` (32-byte salt + 32-byte
digest). The app now stores an algorithm-tagged string --
"pbkdf2$<base64 of that same blob>" or "argon2id$<argon2-cffi encoded
hash>" -- so verify_password() can support both algorithms side by side
and lazily upgrade an account to Argon2id the moment it next logs in
successfully.

This script re-prefixes any existing raw-blob rows in place. It does not
recompute or change the underlying hash -- a user's existing password
still authenticates exactly as before; only the on-disk *format* changes,
not the credential. Rows that already carry a recognized "pbkdf2$" or
"argon2id$" prefix are left untouched, so the script is safe to re-run.

Usage:
    python scripts/migrate_password_prefix.py [--db-path PATH]
"""

import argparse
import base64
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "hr_review.db"
KNOWN_PREFIXES = (b"pbkdf2$", b"argon2id$")


def migrate(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, password_hash FROM users").fetchall()

        migrated = 0
        for user_id, password_hash in rows:
            raw = password_hash if isinstance(password_hash, bytes) else bytes(password_hash, "utf-8")
            if raw.startswith(KNOWN_PREFIXES):
                continue  # already migrated, or already an argon2id row

            new_value = "pbkdf2$" + base64.b64encode(raw).decode("ascii")
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_value, user_id))
            migrated += 1

        conn.commit()
        return migrated
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="Path to the sqlite database file")
    args = parser.parse_args()

    count = migrate(args.db_path)
    print(f"Migrated {count} legacy password hash(es) in {args.db_path}")

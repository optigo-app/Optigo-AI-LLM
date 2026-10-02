"""Clear all runtime data: chat cache, retrieval index, conversations,
feedback, token usage, and audit logs.

Usage: .\\.venv\\Scripts\\python.exe scripts\\clear_all_data.py

Safe to run while the server is up — DBs are emptied row-wise (WAL-safe)
and the live audit.log is truncated in place rather than deleted.
"""
import sqlite3
from pathlib import Path

import diskcache

CACHE_DIR = Path("cache_data")
LOGS_DIR = Path("logs")

# 1. Clear chat cache (diskcache)
if CACHE_DIR.exists():
    c = diskcache.Cache(str(CACHE_DIR))
    n = len(c)
    c.clear()
    c.close()
    print(f"cache_data: cleared {n} entries")

    # Retrieval FTS index lives in the same dir as a plain sqlite file —
    # diskcache.clear() does NOT remove it. Skip cache.db — that's
    # diskcache's own store (already cleared, and locked while the
    # server is running).
    for f in CACHE_DIR.iterdir():
        if f.is_file() and f.suffix in (".db", ".sqlite", ".sqlite3") and f.name != "cache.db":
            try:
                f.unlink()
                print(f"{f}: deleted")
            except PermissionError:
                print(f"{f}: skipped (in use — stop the server to remove it)")
else:
    print("cache_data: not present")

# 2. Clear every SQLite DB under logs/ (conversations, feedback, token_usage, ...)
for db in sorted(LOGS_DIR.glob("*.db")) if LOGS_DIR.exists() else []:
    conn = sqlite3.connect(str(db), timeout=30)
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for t in tables:
        cnt = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        conn.execute(f'DELETE FROM "{t}"')
        print(f"{db.name}: {t} -> deleted {cnt} rows")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

# 3. Truncate the live audit log, delete rotated backups
for log in sorted(LOGS_DIR.glob("*.log*")) if LOGS_DIR.exists() else []:
    if log.name.endswith(".log"):
        open(log, "w").close()
        print(f"{log.name}: truncated")
    else:  # audit.log.1, audit.log.2, ... rotations
        log.unlink()
        print(f"{log.name}: deleted")

print("Done — all runtime data cleared.")

"""Clear all runtime data: chat cache, conversations, feedback, token usage, audit log.

Usage: .\\.venv\\Scripts\\python.exe scripts\\clear_all_data.py
"""
import sqlite3
import diskcache

# 1. Clear chat cache (diskcache)
c = diskcache.Cache("cache_data")
n = len(c)
c.clear()
c.close()
print(f"cache_data: cleared {n} entries")

# 2. Clear SQLite DBs (conversations, feedback, token_usage)
for db in ["logs/conversations.db", "logs/feedback.db", "logs/token_usage.db"]:
    conn = sqlite3.connect(db, timeout=30)
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for t in tables:
        cnt = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        conn.execute(f'DELETE FROM "{t}"')
        print(f"{db}: {t} -> deleted {cnt} rows")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

# 3. Truncate audit log
open("logs/audit.log", "w").close()
print("logs/audit.log: truncated")

print("Done — all runtime data cleared.")

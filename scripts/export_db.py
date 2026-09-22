"""Export all SQLite DB tables to CSV.

Dumps every table in each project SQLite database to a CSV file under
``exports/<db_name>/<table>.csv``.

Run:
    .\\.venv\\Scripts\\python.exe scripts\\export_db.py
    .\\.venv\\Scripts\\python.exe scripts\\export_db.py --out mydump
    .\\.venv\\Scripts\\python.exe scripts\\export_db.py --db logs/conversations.db
"""
import argparse
import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# All SQLite DBs in the project (relative to repo root).
DEFAULT_DBS = [
    "logs/conversations.db",
    "logs/feedback.db",
    "logs/token_usage.db",
    "cache_data/cache.db",
    "scripts/cache_data/cache.db",
]


def list_tables(con: sqlite3.Connection):
    return [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def export_db(db_path: str, out_root: str) -> int:
    """Export every table in db_path to CSV. Returns total rows written."""
    if not os.path.exists(db_path):
        print("  SKIP (missing): %s" % db_path)
        return 0

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    db_name = os.path.splitext(os.path.basename(db_path))[0]
    # Use the full relative dir path so two DBs with the same filename
    # (e.g. cache_data/cache.db vs scripts/cache_data/cache.db) don't collide.
    rel_dir = os.path.dirname(db_path).replace("/", os.sep).replace("\\", os.sep)
    out_dir = os.path.join(out_root, rel_dir, db_name) if rel_dir else os.path.join(out_root, db_name)
    os.makedirs(out_dir, exist_ok=True)

    total = 0
    for table in list_tables(con):
        rows = con.execute('SELECT * FROM "%s"' % table).fetchall()
        cols = rows[0].keys() if rows else [
            d[1] for d in con.execute('PRAGMA table_info("%s")' % table)
        ]
        csv_path = os.path.join(out_dir, "%s.csv" % table)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(list(cols))
            for r in rows:
                w.writerow([r[c] for c in cols])
        total += len(rows)
        print("  %-28s %5d rows -> %s" % (table, len(rows), csv_path))
    con.close()
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Export SQLite DB tables to CSV.")
    ap.add_argument("--out", default="exports", help="output root folder")
    ap.add_argument(
        "--db",
        action="append",
        help="specific DB path(s); repeatable. Defaults to all project DBs.",
    )
    args = ap.parse_args()

    dbs = args.db or DEFAULT_DBS
    grand = 0
    for db in dbs:
        print("DB: %s" % db)
        grand += export_db(db, args.out)
    print("\nDone. %d total rows exported to '%s/'" % (grand, args.out))


if __name__ == "__main__":
    main()

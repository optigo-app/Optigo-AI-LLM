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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _discover_dbs() -> list:
    """All SQLite DBs under the project (logs/, cache_data/, scripts/cache_data/),
    resolved against the repo root so the script works from any cwd."""
    import glob
    found = []
    for pattern in ("logs/*.db", "cache_data/*.db", "scripts/cache_data/*.db"):
        found.extend(glob.glob(os.path.join(ROOT, pattern)))
    return sorted(found)


DEFAULT_DBS = _discover_dbs()


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
    # Flat output: all CSVs in one folder. The db name (+ parent dir when
    # needed for uniqueness) is embedded in the filename so every file is
    # self-identifying, e.g. cache_data_cache__chat_messages.csv
    rel_dir = os.path.dirname(os.path.relpath(db_path, ROOT))
    rel_dir = rel_dir.replace("/", "_").replace("\\", "_").replace(".", "")
    prefix = f"{rel_dir}_{db_name}" if rel_dir else db_name
    os.makedirs(out_root, exist_ok=True)

    total = 0
    for table in list_tables(con):
        rows = con.execute('SELECT * FROM "%s"' % table).fetchall()
        cols = rows[0].keys() if rows else [
            d[1] for d in con.execute('PRAGMA table_info("%s")' % table)
        ]
        csv_path = os.path.join(out_root, "%s__%s.csv" % (prefix, table))
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

    dbs = [d if os.path.isabs(d) else os.path.join(ROOT, d) for d in (args.db or DEFAULT_DBS)]
    out_root = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    grand = 0
    for db in dbs:
        print("DB: %s" % db)
        grand += export_db(db, out_root)
    print("\nDone. %d total rows exported to '%s/'" % (grand, out_root))


if __name__ == "__main__":
    main()

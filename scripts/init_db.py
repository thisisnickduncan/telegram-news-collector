"""One-time (and safe-to-rerun) DB setup: creates db/ dir and applies schema.sql."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import DB_PATH, SCHEMA_PATH


def main():
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    print(f"Initialized database at {db_path.resolve()}")


if __name__ == "__main__":
    main()

"""Adds the mute_rules table backing the digest's "ignore stories like this" button.

Idempotent -- safe to run repeatedly against an existing database, which matters
because deploys run it unconditionally.

`stories.status` needs no migration: it is already a free-text column whose schema
comment reserved 'muted' from the start, so single-story ignores just start using it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.db import db_cursor


def table_exists(cur, name):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,))
    return cur.fetchone() is not None


def main():
    with db_cursor() as cur:
        if table_exists(cur, "mute_rules"):
            print("mute_rules: already present")
        else:
            cur.execute(
                """CREATE TABLE mute_rules (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       rule TEXT NOT NULL,          -- plain-English category, e.g. "lottery jackpots and prize drawings"
                       source_story_id INTEGER REFERENCES stories(id),
                       source_headline TEXT,        -- what you were looking at when you created it
                       created_at TEXT,
                       active INTEGER NOT NULL DEFAULT 1
                   )"""
            )
            print("mute_rules: created")

        # Partial index: matching only ever reads active rules, and the inactive ones
        # are kept purely so an un-muted category can be explained rather than vanishing.
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_mute_rules_active ON mute_rules(active) WHERE active = 1"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stories_status ON stories(status)")
        print("indexes: ok")

        cur.execute("SELECT COUNT(*) AS n FROM mute_rules WHERE active = 1")
        print(f"active mute rules: {cur.fetchone()['n']}")

        cur.execute("SELECT COUNT(*) AS n FROM stories WHERE status = 'muted'")
        print(f"individually muted stories: {cur.fetchone()['n']}")


if __name__ == "__main__":
    main()

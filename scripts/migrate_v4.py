"""Idempotent schema migration for the no-repeats rework.

Safe to run repeatedly and safe against the live DB while the bot is up -- every step
checks for its own prior application. Run it BEFORE deploying the new code: pipeline.py,
developments.py, duplicates.py and mutes.py all read columns added here.

  stories.duplicate_of          -- the story this one turned out to be a second copy of.
                                   Set with status='duplicate' when duplicates.py finds
                                   the same event arriving under two different search
                                   terms. Nothing is deleted and no sources are moved:
                                   the row is just taken out of circulation.
  stories.development_summary   -- the last thing we told you ABOUT this story after the
  stories.development_at           original send, and when. Together they're the
                                   watermark developments.py reads from, so a story that
                                   develops twice is judged against the second state and
                                   can't re-report the first development forever.
  mute_rules.reason             -- your own words for why you muted a category, captured
                                   by the "why?" question after the button press. Shown
                                   to the screening call, which is what lets it settle the
                                   ambiguous cases the category name alone can't.
  preferences.pending_why_rule_id
  preferences.pending_why_at    -- the outstanding "why?" question, and when it was asked
                                   so it can lapse rather than eventually claiming an
                                   unrelated message.

Nothing is backfilled and nothing is dropped. delivered_source_count keeps its meaning as
a record of what a story was worth when sent -- it just no longer decides whether a
delivered story can come back, because now nothing does except a real development.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import DB_PATH


def _columns(cur, table):
    return {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}


def _add_column(cur, table, column, decl, log):
    if column in _columns(cur, table):
        log.append(f"  = {table}.{column} already present")
        return False
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    log.append(f"  + {table}.{column} added")
    return True


def migrate(db_path=None):
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    log = []

    _add_column(cur, "stories", "duplicate_of", "INTEGER REFERENCES stories(id)", log)
    _add_column(cur, "stories", "development_summary", "TEXT", log)
    _add_column(cur, "stories", "development_at", "TEXT", log)
    _add_column(cur, "mute_rules", "reason", "TEXT", log)
    _add_column(cur, "preferences", "pending_why_rule_id",
                "INTEGER REFERENCES mute_rules(id)", log)
    _add_column(cur, "preferences", "pending_why_at", "TEXT", log)

    # developments.py filters delivered stories on sources fetched since the watermark;
    # without this it scans story_sources for every story ever sent, every run.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_story_sources_fetched "
        "ON story_sources(story_id, fetched_at)"
    )
    log.append("  = indexes ok")

    conn.commit()
    conn.close()
    return log


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    target = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    print(f"Migrating {target} ...")
    for line in migrate(target):
        print(line)
    print("Done.")

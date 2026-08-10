"""Idempotent schema migration for the multi-source digest rework.

Safe to run repeatedly and safe to run against the live DB while the bot is up --
every step checks for its own prior application first. Run it before deploying the
new code, not after: the new digest/pipeline code reads columns added here.

  stories.delivered_at      -- when this story was actually sent to you. Replaces
                               "created this run" as the has-it-been-shown test, which
                               is what lets a single-source story be held back and
                               still be delivered a cycle later once it picks up
                               corroboration (it is no longer 'new' by then).
  stories.delivered_source_count
                            -- how many independent sources it had when sent. A story
                               shown as single-source can come back once it earns real
                               corroboration; one already sent with two stays sent.
  stories.expanded_summary  -- deeper write-up produced when you request more coverage.
  source_bias.rating_source -- 'allsides' or 'mbfc', so an AllSides rating always wins
                               over the broader-but-coarser MBFC one on conflict.
  deep_dive_requests        -- your replies to the end-of-digest prompt, queued until
                               the next run consumes them.

Also re-tags story_sources.bias_rating: it used to hold short codes (L/LL/C/LR/R) plus
the literal "Unrated". The coverage breakdown needs the real category, so rows are
reset to NULL and the next run's tag_untagged_sources() refills them canonically.
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

    _add_column(cur, "stories", "delivered_at", "TEXT", log)
    _add_column(cur, "stories", "expanded_summary", "TEXT", log)
    _add_column(cur, "stories", "delivered_source_count", "INTEGER", log)
    _add_column(cur, "source_bias", "rating_source", "TEXT", log)

    existed = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='deep_dive_requests'"
    ).fetchone()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS deep_dive_requests (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               story_id INTEGER REFERENCES stories(id),
               requested_at TEXT,
               consumed_at TEXT          -- NULL until a digest run acts on it
           )"""
    )
    log.append("  = deep_dive_requests already present" if existed
               else "  + deep_dive_requests created")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_deep_dive_pending "
        "ON deep_dive_requests(consumed_at, story_id)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_story_sources_story ON story_sources(story_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stories_delivered ON stories(delivered_at)")

    # Backfill: every story that already carries a telegram_message_id was sent in a
    # pre-migration digest. Without this they'd all look undelivered and the first run
    # after deploy would re-send months of history.
    changed = cur.execute(
        "UPDATE stories SET delivered_at = COALESCE(last_updated_at, first_seen_at) "
        "WHERE delivered_at IS NULL AND telegram_message_id IS NOT NULL"
    ).rowcount
    log.append(f"  ~ backfilled delivered_at on {changed} previously-sent stories")

    # Old short-code ratings can't express the coverage breakdown -- drop them so the
    # next run re-tags with canonical categories against the widened dataset.
    retag = cur.execute(
        "UPDATE story_sources SET bias_rating = NULL WHERE bias_rating IS NOT NULL"
    ).rowcount
    log.append(f"  ~ cleared {retag} legacy bias_rating values for re-tagging")

    # Summaries where the model answered the prompt instead of the news ("I'm unable to
    # read the content from the URL...") were being sent verbatim as though they were the
    # story. summarizer now rejects these on the way in, but ones already stored would
    # never be revisited -- summarize_pending_stories only touches summary IS NULL.
    # Null them so they get another attempt.
    from news_alert.summarizer import _looks_like_refusal

    cur.execute("SELECT id, summary FROM stories WHERE summary IS NOT NULL")
    bad = [row[0] for row in cur.fetchall() if _looks_like_refusal(row[1])]
    for story_id in bad:
        cur.execute("UPDATE stories SET summary = NULL WHERE id = ?", (story_id,))
    log.append(f"  ~ cleared {len(bad)} non-summary (model refusal) summaries for retry")

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

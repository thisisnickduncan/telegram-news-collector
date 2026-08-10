import json
import sqlite3
from contextlib import contextmanager

from news_alert.config import DB_PATH


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def get_preferences():
    """Returns the singleton preferences row as a dict, or None if unset."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM preferences WHERE id = 1")
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "telegram_chat_id": row["telegram_chat_id"],
        "topics": json.loads(row["topics"]),
        "regions": json.loads(row["regions"]),
        "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
        "excluded_sources": json.loads(row["excluded_sources"]) if row["excluded_sources"] else [],
        "digest_max_stories": row["digest_max_stories"],
    }
